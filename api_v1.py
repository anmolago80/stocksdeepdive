"""
api_v1.py

AI-readiness roadmap (AI_ROADMAP_stocksdeepdive.md), Phase 1: a
read-only, public, CORS-enabled JSON API over the same numbers the
Scanner/Deep Dive pages and /s/<TICKER> snapshot pages show. No AI key
involved anywhere in this module, and no user data ever passes through
it - every value here is already public on the site.

Exposes `api_app`, a separate FastAPI instance server.py mounts at
/api/v1 (so its own routes are written relative to that: "/deep-dive/
{ticker}" becomes "/api/v1/deep-dive/{ticker}" once mounted). A
sub-app rather than routes bolted onto the main `app` for two reasons:
it gets its own CORS policy (GET-only, any origin - safe for read-only
public data, and deliberately NOT applied to the rest of the site,
which never intended to allow cross-origin requests) and its own
OpenAPI document (openapi_url="/openapi.json" -> /api/v1/openapi.json,
exactly the path the roadmap names) without touching the outer app's
docs_url=None/redoc_url=None/openapi_url=None (those stay off on
purpose - see server.py's module docstring).

Every endpoint reads from snapshot_store / scan_store only - both are
populated by the existing nightly scan (scheduler_engine ->
nightly_scan.run_universe_scan -> snapshot_store.build_snapshots_from_
scan), so serving a request here never calls yfinance or any other
live data source. That is also what makes "cached" true almost for
free: the underlying data only changes once a day.
"""

import re
import time
from collections import defaultdict, deque
from datetime import datetime, timezone

import pandas as pd
import yfinance as yf
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware

import scan_store
import scanner_engine
import snapshot_render
import snapshot_store

SITE_NAME = "StocksDeepDive"
ATTRIBUTION = snapshot_render.ATTRIBUTION
DISCLAIMER = snapshot_render.PLAIN_DISCLAIMER

# -----------------------------------
# Rate limiting - a small in-memory sliding window per client IP. This
# process runs as a single uvicorn worker (see server.py's __main__), so
# an in-memory counter is a real, correct limit, not just a best-effort
# one; it resets on redeploy, which is fine for "don't let one caller
# hammer the free public API", not a security control.
# -----------------------------------
_RATE_LIMIT = 60          # requests
_RATE_WINDOW_SECONDS = 60  # per this many seconds, per IP
_hits = defaultdict(deque)


def _client_ip(request: Request) -> str:
    # Railway terminates TLS in front of this service and server.py's
    # uvicorn.run(..., proxy_headers=True, forwarded_allow_ips="*") already
    # resolves request.client to the real client IP from X-Forwarded-For -
    # this is just a defensive fallback if that's ever not the case.
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _check_rate_limit(request: Request):
    ip = _client_ip(request)
    now = time.time()
    q = _hits[ip]
    while q and now - q[0] > _RATE_WINDOW_SECONDS:
        q.popleft()
    if len(q) >= _RATE_LIMIT:
        retry_after = max(1, int(_RATE_WINDOW_SECONDS - (now - q[0])))
        raise HTTPException(
            status_code=429,
            detail=f"Rate limit exceeded ({_RATE_LIMIT} requests/"
                   f"{_RATE_WINDOW_SECONDS}s). Try again in {retry_after}s.",
            headers={"Retry-After": str(retry_after)},
        )
    q.append(now)


# -----------------------------------
# Universe name resolution - the roadmap's path is /api/v1/scan/{universe},
# and a display name like "ASX 200" doesn't belong in a URL path
# unescaped, so accept a slug ("asx-200", "asx_200", case-insensitive)
# and map it back to the exact display name scan_store/scanner_engine use.
# -----------------------------------

def _slug(name):
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


_KNOWN_UNIVERSES = (
    scanner_engine.AUSTRALIA_UNIVERSES + scanner_engine.USA_UNIVERSES
)
_SLUG_TO_UNIVERSE = {_slug(u): u for u in _KNOWN_UNIVERSES}


def _resolve_universe(raw):
    return _SLUG_TO_UNIVERSE.get(_slug(raw))


# -----------------------------------
# Response envelope - every payload carries attribution, an as_of
# timestamp and the disclaimer, per the roadmap's "attribution +
# disclaimer in every response" rule.
# -----------------------------------

def _envelope(data, as_of=None, link=None):
    out = {
        "data": data,
        "attribution": ATTRIBUTION,
        "disclaimer": DISCLAIMER,
        "as_of": as_of,
    }
    if link:
        out["link"] = link
    return out


def _snapshot_payload(snap, base_url=""):
    # Fix 6, AI fixes round 2 (2026-08-31): public_view() whitelists and
    # renames the internal row's fields - see snapshot_store.py's own
    # docstring. Signal/Trade Setup/Trend are dropped here (they read as
    # recommendations on a public API response); moat is untouched.
    return {
        "ticker": snap["ticker"],
        "universe": snap.get("universe"),
        "generated_at": snap.get("generated_at"),
        **snapshot_store.public_view(snap.get("data") or {}),
        "moat": snap.get("moat"),
    }


def _public_scan_row(row):
    """Same public_view() mapping as _snapshot_payload, applied to one
    raw scan_store row (nightly_scan.analyze_ticker_lite()'s own return
    shape - scan_store rows were never passed through snapshot_store's
    save/get round trip, so they need the same treatment applied
    directly here). Preserves rank order - callers already sorted
    `rows` by the (now-dropped) internal Long Score before this runs."""
    return {"ticker": row.get("Ticker"), **snapshot_store.public_view(row)}


# -----------------------------------
# Price history - GET /api/v1/history/{ticker} (added <date>). Daily
# closes + MA50, live via yfinance - the one endpoint in this file that
# does NOT read from snapshot_store/scan_store, because neither store
# keeps a day-by-day price series: the nightly scan only ever keeps the
# LATEST MA50 value (deep_dive_engine.analyze()/nightly_scan.py's own
# Fear/Greed calc, rolling(50).mean().iloc[-1]), never the full series
# this endpoint plots.
#
# This sub-app runs inside server.py's own FastAPI process (see this
# module's own docstring). app.py's get_price_history()/_fetch_with_retry
# - the "existing shared fetcher" - live in a DIFFERENT process entirely:
# app.py only ever runs as a `streamlit run app.py` subprocess server.py
# launches and reverse-proxies to (see server.py's own _proxy()), so
# importing app.py from here isn't just discouraged, it's not possible -
# there's no app.py module loaded in this process to import. nightly_
# scan.py hit this exact same wall for its own standalone (non-Streamlit)
# price fetch and duplicated the yfinance call rather than importing
# app.py (see nightly_scan.py's own docstring on tk.history(period=
# "6mo") there: "duplicated rather than imported since [get_price_
# history] is Streamlit-cache-coupled and this function also runs
# standalone, outside any Streamlit script") - same precedent followed
# here: same fetch call, same empty-DataFrame fallback shape. The retry/
# backoff loop below mirrors app.py's own _fetch_with_retry in behaviour
# (3 attempts, 0.5s * attempt-number sleep between them) rather than
# nightly_scan's own single-attempt version, since nightly_scan can
# afford to just skip a ticker in a batch of hundreds on a hiccup, while
# a live public API request is answering one visitor's click and
# deserves the same self-healing retry app.py gives its own UI.
# -----------------------------------

_HISTORY_CACHE_TTL = 1800  # seconds - matches get_price_history's own @st.cache_data(ttl=1800)
_HISTORY_CACHE_MAX = 2000  # safety cap - see _fetch_price_history_df's own comment
_history_cache = {}  # ticker -> (fetched_at_epoch, DataFrame)

# Same shape as server.py's own _TICKER_RE - duplicated rather than
# imported to avoid a circular import (server.py imports THIS module to
# mount it; api_v1.py importing server.py back would invert that).
_HISTORY_TICKER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.\-]{0,14}$")


def _fetch_price_history_df(ticker):
    """~6 months of daily OHLCV via yfinance, with the same retry/backoff
    shape as app.py's _fetch_with_retry and a small per-ticker in-memory
    TTL cache (1800s, matching get_price_history's own @st.cache_data
    ttl) - see the section comment above for why this duplicates rather
    than imports app.py's version. Returns (DataFrame, fetched_at_epoch);
    the DataFrame is empty (never None) on total failure, exactly like
    app.py's own fallback - this never raises.

    _HISTORY_CACHE_MAX: this cache has no per-entry eviction on its own
    (unlike _hits above, which self-trims by time window on every read) -
    a public endpoint taking arbitrary ticker strings could otherwise
    grow it unbounded over a long uptime. Once the cache holds more than
    _HISTORY_CACHE_MAX tickers, this sweeps out fully-expired entries
    before adding a new one - cheap, and keeps this single-worker
    process's memory bounded without needing a background thread or an
    external cache."""
    now = time.time()
    cached = _history_cache.get(ticker)
    if cached and now - cached[0] < _HISTORY_CACHE_TTL:
        return cached[1], cached[0]
    df = pd.DataFrame()
    for attempt in range(3):
        try:
            fetched = yf.Ticker(ticker).history(period="6mo")
            if fetched is not None and not fetched.empty:
                df = fetched
                break
        except Exception:
            df = pd.DataFrame()
        if attempt < 2:
            time.sleep(0.5 * (attempt + 1))
    if len(_history_cache) > _HISTORY_CACHE_MAX:
        for k in [k for k, v in _history_cache.items() if now - v[0] >= _HISTORY_CACHE_TTL]:
            del _history_cache[k]
    _history_cache[ticker] = (now, df)
    return df, now


def _infer_currency(ticker):
    """Same suffix convention app.py's own _pos_default_currency already
    uses (".AX" -> AUD, else USD) - avoids a second live yfinance .info
    call just for a currency code on an endpoint whose whole point is
    price history, not fundamentals. Same limitation as that existing
    helper: not accurate for other non-US exchange suffixes (.L, .TO,
    etc.) - pre-existing gap in the codebase's convention, not new here."""
    return "AUD" if ticker.upper().endswith(".AX") else "USD"


# -----------------------------------
# The sub-app
# -----------------------------------

api_app = FastAPI(
    title=f"{SITE_NAME} API",
    version="v1",
    description=(
        f"Read-only, public JSON API over {SITE_NAME}'s computed stock "
        "scores (value, quality, psychology, discovery, moat). No "
        "authentication needed; GET only; rate-limited; no user data is "
        "ever served here. See /api for human-readable docs and "
        "attribution terms."
    ),
    openapi_url="/openapi.json",
    docs_url="/docs",
    redoc_url=None,
)

api_app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)


@api_app.get("/deep-dive/{ticker}", summary="Computed scores for one ticker")
def get_deep_dive(ticker: str, request: Request):
    """The same value/quality/psychology/discovery/moat numbers the public
    Deep Dive and /s/<ticker> snapshot page show, computed by the nightly
    scan (not live on this request - see module docstring)."""
    _check_rate_limit(request)
    ticker = (ticker or "").strip().upper()
    snap = snapshot_store.get_snapshot(ticker)
    if not snap:
        raise HTTPException(
            status_code=404,
            detail=f"No snapshot for '{ticker}' - it may not be in a "
                   "covered universe yet, or hasn't been scanned. See "
                   "/api/v1/scan/{universe} for what's covered.",
        )
    base = str(request.base_url).rstrip("/")
    return _envelope(
        _snapshot_payload(snap),
        as_of=snap.get("generated_at"),
        link=snapshot_render.snapshot_url(base, ticker),
    )


@api_app.get("/compare", summary="Computed scores for several tickers side by side")
def get_compare(
    request: Request,
    tickers: str = Query(..., description="Comma-separated tickers, e.g. CSL.AX,BHP.AX (max 10)"),
):
    _check_rate_limit(request)
    wanted = [t.strip().upper() for t in tickers.split(",") if t.strip()]
    if not wanted:
        raise HTTPException(status_code=400, detail="Pass at least one ticker in ?tickers=")
    if len(wanted) > 10:
        raise HTTPException(status_code=400, detail="Max 10 tickers per request")
    base = str(request.base_url).rstrip("/")
    results, missing = [], []
    for t in wanted:
        snap = snapshot_store.get_snapshot(t)
        if snap:
            results.append(_snapshot_payload(snap))
        else:
            missing.append(t)
    return _envelope({"tickers": results, "not_found": missing}, link=f"{base}/comparison?tickers={','.join(wanted)}")


@api_app.get("/scan/{universe}", summary="Ranked overnight scan for a universe")
def get_scan(universe: str, request: Request):
    """The saved overnight scan for a whole index/universe (e.g. ASX 200),
    ranked by Value Score - the same data the Scanner page shows on load.
    `universe` is a slug: asx-200, asx-300, sp-500, nasdaq-100,
    russell-2000, small-caps-sp-600."""
    _check_rate_limit(request)
    resolved = _resolve_universe(universe)
    if not resolved:
        raise HTTPException(
            status_code=404,
            detail=f"Unknown universe '{universe}'. Known: "
                   + ", ".join(sorted(_SLUG_TO_UNIVERSE)),
        )
    payload = scan_store.load_scan(resolved)
    if not payload:
        raise HTTPException(
            status_code=404,
            detail=f"No saved scan for '{resolved}' yet - it may not be in "
                   "NIGHTLY_UNIVERSES, or the first scan hasn't completed.",
        )
    base = str(request.base_url).rstrip("/")
    return _envelope(
        {
            "universe": resolved,
            "source": payload.get("source"),
            "rows": [_public_scan_row(r) for r in (payload.get("rows") or [])],
        },
        as_of=payload.get("generated_at"),
        link=f"{base}/scanner",
    )


@api_app.get("/history/{ticker}", summary="~6 months of daily closes plus the 50-day moving average")
def get_history(ticker: str, request: Request):
    """Daily closing prices for `ticker` over ~6 months, plus the 50-day
    moving average over the same window - the same underlying price feed
    (yfinance's .history(period="6mo")) app.py's own MA50/Fear-Greed/Deep
    Dive calculations use. See the section comment above
    _fetch_price_history_df for why this is a duplicated fetch call
    rather than a shared import (app.py runs in a separate process).

    Fails soft, always 200: an unknown ticker, an empty fetch (yfinance
    hiccup, delisted symbol) or a malformed ticker string all return the
    same envelope shape with empty points/ma50 lists rather than a
    404/422/500. This is deliberately different from this file's other
    endpoints (deep-dive/compare/scan), which raise a clean 404 for a
    genuinely unknown ticker/universe - those read from a store populated
    once a day, where "not in the store" is an unambiguous, stable fact.
    This endpoint calls a live external service on every cache miss
    instead, where "no data" and "temporarily unavailable" aren't
    reliably distinguishable request-to-request - so degrading to an
    empty-but-well-formed envelope is the safer default here, per this
    task's own explicit "never a 500" requirement. Every float below is
    rounded (never NaN/Infinity) before this returns - see snapshot_
    store.public_view's own docstring for why that specifically matters
    on this API: Starlette's JSONResponse calls json.dumps(...,
    allow_nan=False), so an unrounded/unfiltered NaN slipping through
    would itself be a 500, the exact thing this endpoint promises not to
    do."""
    _check_rate_limit(request)
    ticker = (ticker or "").strip().upper()
    empty = {"ticker": ticker, "currency": None, "points": [], "ma50": []}
    if not _HISTORY_TICKER_RE.match(ticker):
        return _envelope(empty)
    df, fetched_at = _fetch_price_history_df(ticker)
    if df is None or df.empty or "Close" not in df.columns:
        return _envelope(empty)
    closes = df["Close"].dropna()
    if closes.empty:
        return _envelope(empty)
    points = [[idx.strftime("%Y-%m-%d"), round(float(v), 4)] for idx, v in closes.items()]
    ma50_series = closes.rolling(50).mean().dropna()
    ma50 = [[idx.strftime("%Y-%m-%d"), round(float(v), 4)] for idx, v in ma50_series.items()]
    base = str(request.base_url).rstrip("/")
    return _envelope(
        {
            "ticker": ticker,
            "currency": _infer_currency(ticker),
            "points": points,
            "ma50": ma50,
        },
        as_of=datetime.fromtimestamp(fetched_at, tz=timezone.utc).isoformat(),
        link=snapshot_render.snapshot_url(base, ticker),
    )
