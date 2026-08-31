"""
mcp_server.py

AI-readiness roadmap (AI_ROADMAP_stocksdeepdive.md), Phase 2: an MCP
(Model Context Protocol) server exposing the same read-only, public data
Phase 1's /api/v1/* JSON API serves, as callable tools for AI assistants
(Claude, ChatGPT and other MCP clients) instead of raw HTTP endpoints. No
AI key involved anywhere in this module: every tool below reads from
snapshot_store / scan_store / moat_engine / blog_store only - the exact
same nightly-computed data Phase 1 already serves publicly - so exposing
it here adds no new network calls and no new cost.

Exposes `mcp`, a FastMCP instance, mounted into server.py's FastAPI app
as a Streamable HTTP sub-app at /mcp:

    app.mount("/mcp", mcp_server.mcp.streamable_http_app())

`streamable_http_path="/"` below means the sub-app's own single route is
"/", so mounting it at "/mcp" serves the protocol at exactly /mcp - not
/mcp/mcp. `stateless_http=True` because every tool here is a pure read
with no per-session state worth keeping; it also means server.py's
lifespan doesn't need to track session cleanup, just enter/exit the
session manager's own async context (see server.py's lifespan function -
Starlette does not propagate a parent app's lifespan into a mounted
sub-app automatically, so that has to be done explicitly there).

Never a second Railway service, matching every other background job in
this codebase (see scheduler_engine.py's own docstring for the same
reasoning: one service, one volume, zero cross-service coordination).

Every tool result carries attribution, an as_of timestamp, the
disclaimer and a link back to the relevant page - exactly like Phase 1's
JSON API envelope - so an assistant surfacing this data to a person can
always show where it came from and link them to the live page.
"""

import os

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

import blog_render
import blog_store
import scan_store
import scanner_engine
import snapshot_render
import snapshot_store

SITE_NAME = snapshot_render.SITE_NAME
ATTRIBUTION = snapshot_render.ATTRIBUTION
DISCLAIMER = snapshot_render.PLAIN_DISCLAIMER

# Duplicated from server.py's own DEFAULT_BASE_URL rather than imported -
# server.py imports this module to mount it, so importing server.py back
# from here would be circular. Two lines is cheaper than restructuring
# either file (api_v1.py takes the same approach with _client_ip).
BASE_URL = os.environ.get("PUBLIC_BASE_URL", "https://stocksdeepdive.com").rstrip("/")


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


def _snapshot_payload(snap):
    return {
        "ticker": snap["ticker"],
        "universe": snap.get("universe"),
        "generated_at": snap.get("generated_at"),
        **(snap.get("data") or {}),
        "moat": snap.get("moat"),
    }


_KNOWN_UNIVERSES = scanner_engine.AUSTRALIA_UNIVERSES + scanner_engine.USA_UNIVERSES


def _resolve_universe(raw):
    """Accepts a display name ("ASX 200"), case-insensitively, or the same
    slug api_v1.py's /api/v1/scan/{universe} uses ("asx-200") - an AI
    client is just as likely to pass either."""
    if not raw:
        return None
    raw_norm = raw.strip().lower()
    for u in _KNOWN_UNIVERSES:
        if u.lower() == raw_norm:
            return u
    import re
    slug = re.sub(r"[^a-z0-9]+", "-", raw_norm).strip("-")
    for u in _KNOWN_UNIVERSES:
        if re.sub(r"[^a-z0-9]+", "-", u.lower()).strip("-") == slug:
            return u
    return None


mcp = FastMCP(
    name=SITE_NAME,
    instructions=(
        f"Read-only, public data from {SITE_NAME} (stocksdeepdive.com): "
        "computed value/quality/psychology/discovery/moat scores for "
        "stocks the site's nightly scan covers, plus any research notes "
        "written about a company. No authentication needed, and nothing "
        "here is user data - every value returned is already public on "
        "the site. Every tool result includes an 'attribution', "
        "'disclaimer' and 'link' field; when you show this data to a "
        "person, attribute it and include the link. Nothing returned "
        "here is financial advice - see the 'disclaimer' field in every "
        "result. A ticker only has data once it has been through a "
        "nightly scan; call scan() first if you're not sure a ticker is "
        "covered."
    ),
    streamable_http_path="/",
    stateless_http=True,
    # Fix 1, round 1 (2026-08-31): the mcp 1.x Streamable-HTTP transport's
    # DNS-rebinding protection defaults to trusting only localhost/
    # 127.0.0.1 Host headers - every request behind Railway arrives with
    # Host: stocksdeepdive.com (or www.), so every /mcp call was getting
    # a flat 421 "Invalid Host header", meaning no external AI client
    # could ever connect. Listing the real production hosts/origins here
    # fixes that without disabling the protection outright.
    #
    # NOT included: a "*.up.railway.app" wildcard entry (present in an
    # earlier draft of this fix). Checked against the installed mcp
    # package's own TransportSecurityMiddleware._validate_host (mcp
    # 1.27.0, mcp/server/transport_security.py): its only wildcard form
    # is a ":*" PORT suffix ("localhost:*" matches any port on
    # localhost) - there is no "*." subdomain-prefix wildcard support at
    # all, so a literal "*.up.railway.app" entry would only ever match a
    # Host header that is the literal 8-character string "*.up.railway.
    # app", which no real request sends. It was dead weight, not a
    # working rule. It's also unnecessary here: `list-domains` on the
    # live Railway service shows no active Railway-generated
    # service domain, only the two custom domains below - the site is
    # reached exclusively via stocksdeepdive.com / www.stocksdeepdive.com.
    # If a Railway subdomain is ever added back, list its exact hostname
    # (e.g. "stocksdeepdive-production.up.railway.app") here explicitly
    # rather than a non-functional wildcard.
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[
            "stocksdeepdive.com", "www.stocksdeepdive.com",
            "localhost:*", "127.0.0.1:*",
        ],
        allowed_origins=[
            "https://stocksdeepdive.com", "https://www.stocksdeepdive.com",
            "http://localhost:*", "http://127.0.0.1:*",
        ],
    ),
)


@mcp.tool()
def deep_dive(ticker: str) -> dict[str, object]:
    """Computed value/quality/psychology/discovery/moat scores for one
    stock, from StocksDeepDive's nightly scan - the same numbers its
    public Deep Dive and /s/<ticker> snapshot page show. Raises if the
    ticker hasn't been scanned yet (it may not be in a covered universe -
    call scan() to see what's covered)."""
    ticker = (ticker or "").strip().upper()
    snap = snapshot_store.get_snapshot(ticker)
    if not snap:
        raise ValueError(
            f"No snapshot for '{ticker}' - it may not be in a covered "
            "universe yet, or hasn't been scanned. Call scan() to see "
            "what's covered."
        )
    return _envelope(
        _snapshot_payload(snap),
        as_of=snap.get("generated_at"),
        link=snapshot_render.snapshot_url(BASE_URL, ticker),
    )


@mcp.tool()
def compare(tickers: list[str]) -> dict[str, object]:
    """Computed scores for several stocks side by side (max 10 tickers).
    Tickers not yet scanned are listed separately under 'not_found'
    rather than raising, so a partial comparison still returns."""
    wanted = [(t or "").strip().upper() for t in (tickers or []) if (t or "").strip()]
    if not wanted:
        raise ValueError("Pass at least one ticker.")
    if len(wanted) > 10:
        raise ValueError("Max 10 tickers per call.")
    results, missing = [], []
    for t in wanted:
        snap = snapshot_store.get_snapshot(t)
        if snap:
            results.append(_snapshot_payload(snap))
        else:
            missing.append(t)
    return _envelope(
        {"tickers": results, "not_found": missing},
        link=f"{BASE_URL}/comparison?tickers={','.join(wanted)}",
    )


@mcp.tool()
def scan(universe: str, top_n: int = 25) -> dict[str, object]:
    """The ranked overnight scan for a whole index/universe (e.g.
    "ASX 200", "S&P 500", "Nasdaq 100", "Russell 2000",
    "Small Caps (S&P 600)", "ASX 300"), highest Long Score first - the
    same list the public Scanner page shows on load. top_n caps how many
    rows come back (default 25, max 100); pass a display name or a
    hyphenated slug, either works."""
    resolved = _resolve_universe(universe)
    if not resolved:
        raise ValueError(
            f"Unknown universe '{universe}'. Known: "
            + ", ".join(_KNOWN_UNIVERSES)
        )
    payload = scan_store.load_scan(resolved)
    if not payload:
        raise ValueError(
            f"No saved scan for '{resolved}' yet - the first nightly "
            "scan for it may not have completed."
        )
    top_n = max(1, min(int(top_n or 25), 100))
    rows = (payload.get("rows") or [])[:top_n]
    return _envelope(
        {"universe": resolved, "source": payload.get("source"), "rows": rows},
        as_of=payload.get("generated_at"),
        link=f"{BASE_URL}/scanner",
    )


@mcp.tool()
def moat(ticker: str) -> dict[str, object]:
    """The cached Moat Score (0-100, competitive-advantage durability)
    for one stock, plus its erosion flag ("none"/"watch"/"eroding") -
    the same figure shown on the Deep Dive page and in deep_dive()'s
    result. Read-only: this tool never triggers a fresh computation, so
    a ticker whose Deep Dive page nobody has opened yet (or whose moat
    the nightly scan hasn't attached) returns moat: null rather than
    computing one live - open its Deep Dive page on the site first, or
    call deep_dive() which returns whatever Moat the nightly scan already
    attached."""
    ticker = (ticker or "").strip().upper()
    if not ticker:
        raise ValueError("Pass a ticker.")
    snap = snapshot_store.get_snapshot(ticker)
    moat_data = snap.get("moat") if snap else None
    if moat_data is None:
        import moat_engine
        try:
            moat_data = moat_engine.get_cached_moat(ticker)
        except Exception:
            moat_data = None
    return _envelope(
        {"ticker": ticker, "moat": moat_data},
        as_of=snap.get("generated_at") if snap else None,
        link=snapshot_render.snapshot_url(BASE_URL, ticker) if snap else f"{BASE_URL}/deep-dive?ticker={ticker}",
    )


@mcp.tool()
def research_notes(ticker: str) -> dict[str, object]:
    """Published research notes (blog posts) StocksDeepDive has written
    specifically about this company, newest first - title, summary,
    publish date and a link to the full post. Empty list if none exist
    yet; that's not an error, most tickers won't have a dedicated post."""
    ticker = (ticker or "").strip().upper()
    if not ticker:
        raise ValueError("Pass a ticker.")
    posts = blog_store.posts_for_ticker(ticker, include_drafts=False)
    notes = [
        {
            "title": p["title"],
            "summary": p.get("summary") or "",
            "published_at": p.get("published_at"),
            "author": p.get("author") or "",
            "url": blog_render.post_url(BASE_URL, p["slug"]),
        }
        for p in posts
    ]
    return _envelope(
        {"ticker": ticker, "notes": notes},
        link=f"{BASE_URL}/blog",
    )
