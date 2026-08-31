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
