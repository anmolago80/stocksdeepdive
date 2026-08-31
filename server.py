"""
server.py - the process Railway actually starts.

WHY THIS EXISTS
---------------
StocksDeepDive is a Streamlit app. Streamlit sends the browser a
JavaScript shell and then streams every widget and every word of content
over a websocket. That is fine for a tool and fatal for search: a crawler
fetching https://stocksdeepdive.com/research gets an empty shell with one
generic <title> and no article text, and there is no way to serve
/robots.txt or /sitemap.xml from inside Streamlit at all. No Streamlit
page on this site can meaningfully be indexed.

So the blog is not built inside Streamlit. This module puts a thin ASGI
server in front of it:

    browser ──► server.py (this file, on $PORT)
                  ├─ /blog, /blog/<slug>, /blog/media/…   ← real HTML, rendered here
                  ├─ /sitemap.xml, /robots.txt, /feed.xml ← rendered here
                  └─ everything else ─────────────────────► Streamlit on 127.0.0.1:8501
                                                             (HTTP and websocket, proxied)

The app is untouched by a /blog request and behaves exactly as before on
every other URL. Posts get real URLs on the main domain
(stocksdeepdive.com/blog/<slug>) rather than a subdomain, so the links
they earn accrue to the domain the tools live on.

HEADER FIDELITY MATTERS
-----------------------
Streamlit's websocket handler accepts a connection when the Origin header
matches the Host header. The proxy therefore forwards the browser's real
Host and Origin upstream (for the websocket, by keeping the public host in
the URI and overriding only the TCP target), instead of the usual trick of
switching off Streamlit's CORS and XSRF protection. Same protection as
before, no config weakening.

ENV
---
PORT                            public port (set by Railway)
STREAMLIT_INTERNAL_PORT         where Streamlit is run  (default 8501)
PUBLIC_BASE_URL                 canonical origin used in canonical tags,
                                sitemap and feed (default
                                https://stocksdeepdive.com)
GOOGLE_SITE_VERIFICATION_FILE   e.g. google1a2b3c.html - served for Search
                                Console's HTML-file verification
GOOGLE_SITE_VERIFICATION_TAG    alternative: the meta-tag content value
DEFAULT_OG_IMAGE                absolute URL used as the social card image
                                for posts with no hero image
"""

import asyncio
import hmac
import logging
import os
import re
import signal
import subprocess
import sys
import time
from contextlib import AsyncExitStack, asynccontextmanager, suppress
from urllib.parse import urlparse

import httpx
from fastapi import FastAPI, Request, Response, WebSocket
from fastapi.responses import (FileResponse, HTMLResponse, PlainTextResponse,
                               RedirectResponse, StreamingResponse)
from fastapi.staticfiles import StaticFiles

import blog_comments_store
import blog_render
import blog_store
import email_auth
import push_send
import push_store
import site_content

# AI-readiness roadmap Phase 1 (AI_ROADMAP_stocksdeepdive.md): the public
# snapshot pages + read-only JSON API. See api_v1.py / snapshot_render.py.
import api_v1
import snapshot_render
import snapshot_store

# AI-readiness roadmap Phase 2: the MCP server exposing the same data as
# callable tools for AI assistants. See mcp_server.py.
import mcp_server

# AI-readiness roadmap Phase 4: citation helpers (llms.txt, the
# track-record page, author/organisation schema - the schema itself lives
# in blog_render.py/snapshot_render.py, reused as-is; only the new
# /track-record page needs its own render module and score_history read).
import score_history
import track_record_render

try:
    import metrics_store
except Exception:  # analytics must never be able to stop the site serving
    metrics_store = None

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("sdd.server")

PORT = int(os.environ.get("PORT", "8080"))
STREAMLIT_PORT = int(os.environ.get("STREAMLIT_INTERNAL_PORT", "8501"))
STREAMLIT_HOST = "127.0.0.1"
UPSTREAM = f"http://{STREAMLIT_HOST}:{STREAMLIT_PORT}"
DEFAULT_BASE_URL = os.environ.get(
    "PUBLIC_BASE_URL", "https://stocksdeepdive.com").rstrip("/")

# Streamlit's own app script - unchanged, still the whole app.
APP_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "app.py")

# PWA static assets (icons, offline fallback) - see scripts/generate_icons.py
# for the icons themselves. On disk this is still ./static (matches the
# brief), but it is deliberately mounted at URL prefix /pwa, NOT /static -
# Streamlit's own shell references its JS/CSS bundle at "./static/js/..."
# and "./static/css/..." relative to "/", i.e. at the URL /static/js/... on
# this site. Mounting our own assets at /static shadows that path entirely
# (StaticFiles 404s before the request ever reaches the proxy to Streamlit)
# and silently breaks the whole app - blank, unstyled page, no JS. Caught
# by hand-testing every route after wiring this up; do not rename this
# mount back to /static.
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

# Headers that describe one TCP hop and must not be copied to the next one.
HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade",
}

_streamlit_proc = None
_client: httpx.AsyncClient | None = None


# -----------------------------------
# STREAMLIT SUBPROCESS
# -----------------------------------

def _start_streamlit():
    """Launch Streamlit bound to loopback only. Nothing outside this
    container can reach it directly - every request arrives through the
    proxy below."""
    cmd = [
        sys.executable, "-m", "streamlit", "run", APP_SCRIPT,
        "--server.port", str(STREAMLIT_PORT),
        "--server.address", STREAMLIT_HOST,
        "--server.headless", "true",
        "--browser.gatherUsageStats", "false",
        # Streamlit is behind a proxy on the same machine; without this it
        # would keep trying to work out an external address for itself.
        "--server.fileWatcherType", "none",
    ]
    log.info("starting Streamlit: %s", " ".join(cmd))
    return subprocess.Popen(cmd, env=os.environ.copy())


async def _wait_for_streamlit(timeout=180):
    """Block until Streamlit answers its own health endpoint. The app
    imports pandas, yfinance, plotly and friends, so first boot is slow;
    serving proxy traffic before it is ready would show visitors a
    connection error instead of a loading app."""
    deadline = time.time() + timeout
    async with httpx.AsyncClient(timeout=5) as probe:
        while time.time() < deadline:
            if _streamlit_proc and _streamlit_proc.poll() is not None:
                log.error("Streamlit exited during startup (code %s)",
                          _streamlit_proc.returncode)
                return False
            try:
                r = await probe.get(f"{UPSTREAM}/_stcore/health")
                if r.status_code == 200:
                    log.info("Streamlit is up on port %s", STREAMLIT_PORT)
                    return True
            except Exception:
                pass
            await asyncio.sleep(0.5)
    log.error("Streamlit did not become healthy within %ss", timeout)
    return False


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _streamlit_proc, _client
    blog_store.ensure_media_dir()
    # One-time-per-post inference for posts that predate primary_ticker
    # (P3.2) - deterministic given the same title, so safe on every
    # startup; see blog_store.backfill_primary_tickers()'s own docstring.
    # Never allowed to stop the site serving.
    with suppress(Exception):
        blog_store.backfill_primary_tickers()
    _client = httpx.AsyncClient(
        base_url=UPSTREAM, timeout=httpx.Timeout(None, connect=10.0),
        follow_redirects=False, limits=httpx.Limits(max_connections=200),
    )
    _streamlit_proc = _start_streamlit()
    # Don't block startup on it: /blog must answer even while the app is
    # still importing, and Railway's own health probe shouldn't time out.
    asyncio.create_task(_wait_for_streamlit())
    # AI-readiness roadmap Phase 2: mcp_server.mcp is mounted below as a
    # Streamable HTTP sub-app (app.mount("/mcp", ...)), but Starlette does
    # NOT propagate this app's lifespan into a mounted sub-app on its own -
    # its session manager only starts accepting requests once its own
    # `.run()` async context has been entered. AsyncExitStack lets that
    # happen inside this same lifespan without restructuring the function
    # into nested `async with` blocks; it's exited automatically at
    # shutdown, right alongside everything below.
    async with AsyncExitStack() as mcp_stack:
        await mcp_stack.enter_async_context(mcp_server.mcp.session_manager.run())
        try:
            yield
        finally:
            if _client:
                await _client.aclose()
            if _streamlit_proc and _streamlit_proc.poll() is None:
                log.info("stopping Streamlit")
                _streamlit_proc.send_signal(signal.SIGTERM)
                with suppress(Exception):
                    _streamlit_proc.wait(timeout=10)


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/pwa", StaticFiles(directory=STATIC_DIR), name="pwa_static")


# -----------------------------------
# HELPERS
# -----------------------------------

def _base_url(request: Request) -> str:
    """Canonical origin for links written into HTML/XML. Pinned to
    PUBLIC_BASE_URL by default so a page served on www. and on the apex
    still declares one canonical address (duplicate content across two
    hostnames splits ranking signals); falls back to the request's own
    origin when the env var is cleared, which is what makes local testing
    work."""
    if DEFAULT_BASE_URL:
        return DEFAULT_BASE_URL
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = request.headers.get("host", f"localhost:{PORT}")
    return f"{proto}://{host}"


def _is_https(request: Request) -> bool:
    return request.headers.get("x-forwarded-proto", request.url.scheme) == "https"


def _same_origin(request: Request) -> bool:
    """True only when the request's own Origin (or, failing that, Referer)
    header names THIS site's own host - a real fetch() call from a page
    served by this app always sends one of these; a cross-site request
    (an <img>/<form> from another page, or a bare curl) generally won't
    have a matching one. Used to gate the auth-cookie-setting endpoints
    below against CSRF/session-fixation (see their own docstrings)."""
    origin = request.headers.get("origin") or request.headers.get("referer") or ""
    host = request.headers.get("host", "")
    return bool(host) and host in origin


def _signed_in_email(request: Request):
    """The visitor's signed-in email, resolved directly from the sdd_auth
    cookie (email_auth.session_email) - completely independent of
    Streamlit's own session state, since this process never talks to
    Streamlit for these calls. Used only by the /push/* routes below."""
    tok = request.cookies.get("sdd_auth")
    if not tok:
        return None
    try:
        return email_auth.session_email(tok)
    except Exception:
        return None


def _client_ip(request: Request) -> str:
    """Best-effort visitor IP for the comment rate-limit hash - Railway
    sits this process behind a proxy, so the socket peer (request.client)
    is the proxy, not the visitor; X-Forwarded-For's first hop is the
    real one when present."""
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else ""


# Audit fix (3.2): paywall_engine.write_auth_cookie() previously wrote the
# 90-day sdd_auth session cookie via client-side `document.cookie=...`
# (the only option available from inside Streamlit itself, which can't set
# real response headers) - meaning it had no HttpOnly flag and was fully
# readable by any JS on the page. That's a structural risk: a future XSS
# ANYWHERE on the site (this codebase's own blog CMS renders admin-authored
# HTML - see the admin-key hardening above) would be an immediate full
# account-takeover, not just page defacement, since the token itself is
# the only thing protecting a signed-in session. This process (server.py)
# DOES control real HTTP response headers, and proxies every request to
# the same origin Streamlit serves from - so Streamlit can fire a same-
# origin fetch() here instead of writing the cookie itself, and let this
# process set it properly. The session token's own generation/validation
# in email_auth.py is completely unchanged by this - only HOW the browser
# ends up holding it changes.
#
# POST (not GET) + the _same_origin() check together are what stop this
# becoming a session-fixation hole: a GET endpoint that sets a cookie from
# a plain query param would be triggerable from a bare <img src=...> tag
# planted on any other website, letting an attacker fixate a victim's
# cookie to a token the attacker already controls.
@app.post("/_auth/set-cookie", include_in_schema=False)
async def auth_set_cookie(request: Request):
    if not _same_origin(request):
        return Response(status_code=403)
    try:
        body = await request.json()
    except Exception:
        body = {}
    tok = (body.get("tok") or "").strip()
    if not tok or len(tok) > 256:
        return Response(status_code=400)
    resp = Response(status_code=204)
    resp.set_cookie("sdd_auth", tok, max_age=90 * 24 * 3600, path="/",
                     httponly=True, secure=_is_https(request), samesite="lax")
    return resp


@app.post("/_auth/clear-cookie", include_in_schema=False)
async def auth_clear_cookie(request: Request):
    if not _same_origin(request):
        return Response(status_code=403)
    resp = Response(status_code=204)
    resp.delete_cookie("sdd_auth", path="/")
    return resp


def _html(content, status=200, cache="public, max-age=300"):
    return HTMLResponse(content, status_code=status,
                        headers={"Cache-Control": cache})


# -----------------------------------
# WHICH PAGES THIS SERVER RENDERS ITSELF
#
# /blog is always served here - it has no Streamlit equivalent. The app's
# own pages are a different matter: rendering them as HTML makes them
# indexable, but it also replaces the live Streamlit page a visitor sees,
# and that is a product decision rather than a technical one. So it is
# OFF by default and turned on per page with an environment variable:
#
#   INDEXABLE_PAGES=all
#   INDEXABLE_PAGES=/methodology,/about,/privacy
#
# Unset (the default), every app URL behaves exactly as it did before this
# server existed: proxied straight to Streamlit.
# -----------------------------------
_INDEXABLE_RAW = os.environ.get("INDEXABLE_PAGES", "").strip()
_INDEXABLE_ALL = _INDEXABLE_RAW.lower() in ("all", "*", "true", "yes")
_INDEXABLE = {p.strip() for p in _INDEXABLE_RAW.split(",") if p.strip()}


def _renders_html(path) -> bool:
    return _INDEXABLE_ALL or path in _INDEXABLE


# Audit fix 4.3: the specific query param keys that mean "this is a real
# use of the app, not a plain visit" - ?ticker=/?tickers= (a Deep
# Dive/Comparison/Scanner lookup), ?admin= (full-view unlock), ?code=/
# ?state= (Google's OAuth callback), ?app=1 (the explicit "give me the
# live app" link), ?src= (article attribution the app records). home()
# and tool_landing() used to gate on "any query string present at all",
# which also caught harmless marketing/tracking params (utm_source,
# utm_medium, gclid, fbclid, ...) - sending exactly the visitors most
# likely to convert (people who just clicked a link) to the slow,
# title-less JS shell instead of the fast indexed static page.
# content_page() already gets this right by ignoring the query entirely
# (its pages never behave differently based on query params).
_STREAMLIT_ONLY_PARAMS = {"ticker", "tickers", "admin", "code", "state", "app", "src"}


def _needs_streamlit(request: Request) -> bool:
    return bool(set(request.query_params.keys()) & _STREAMLIT_ONLY_PARAMS)


def _count_view(page, ticker=None):
    """Keep the admin Stats popover honest. These pages used to be counted
    by app.py's _bump_page_view; now that they are served here, the count
    has to happen here or the numbers silently stop."""
    if not metrics_store:
        return
    try:
        metrics_store.bump(page, ticker=ticker)
    except Exception:
        pass


# Audit fix (3.1): this endpoint is a stateless GET with no session
# overhead - before this fix it was directly brute-forceable from a
# script with zero rate limiting anywhere. Same process-wide lockout
# shape as app.py's admin-key check (see _admin_key_locked_out() there);
# this is a separate process (server.py is the FastAPI proxy, app.py is
# the Streamlit process it launches as a subprocess - see this module's
# own docstring), so it needs its own counter rather than sharing state.
_ADMIN_PREVIEW_LOCKOUT_MAX_ATTEMPTS = 8
_ADMIN_PREVIEW_LOCKOUT_WINDOW_SECONDS = 600
_admin_preview_fail_times: list = []


def _admin_preview_locked_out() -> bool:
    now = time.time()
    while (_admin_preview_fail_times
           and now - _admin_preview_fail_times[0] > _ADMIN_PREVIEW_LOCKOUT_WINDOW_SECONDS):
        _admin_preview_fail_times.pop(0)
    return len(_admin_preview_fail_times) >= _ADMIN_PREVIEW_LOCKOUT_MAX_ATTEMPTS


def _admin_preview_ok(request: Request) -> bool:
    """Draft posts are viewable at their real URL only by the admin - the
    same ADMIN_REFRESH_KEY the app uses, passed as ?preview=<key>. Drafts
    are noindex, absent from the index, sitemap and feed."""
    key = os.environ.get("ADMIN_REFRESH_KEY", "").strip()
    if not key:
        return False
    supplied = (request.query_params.get("preview") or "").strip()
    if not supplied:
        return False
    if _admin_preview_locked_out():
        return False
    if hmac.compare_digest(supplied, key):
        return True
    _admin_preview_fail_times.append(time.time())
    return False


# -----------------------------------
# PWA ROUTES (Part 1/2) - manifest, service worker, favicon.
#
# Registered here, well before the catch-all proxy at the bottom of this
# file, so they answer directly instead of falling through to Streamlit.
# Icons themselves are plain files under STATIC_DIR, served by the
# app.mount("/pwa", ...) above - see scripts/generate_icons.py.
# -----------------------------------

_MANIFEST_JSON = """{
  "name": "StocksDeepDive",
  "short_name": "StocksDD",
  "start_url": "/",
  "scope": "/",
  "display": "standalone",
  "background_color": "#0b1220",
  "theme_color": "#0b1220",
  "description": "Free-data intrinsic value, quality and crowd-psychology research for any ASX or US stock.",
  "icons": [
    {"src": "/pwa/icons/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
    {"src": "/pwa/icons/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"},
    {"src": "/pwa/icons/icon-512-maskable.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"}
  ]
}
"""


@app.get("/manifest.webmanifest", include_in_schema=False)
async def manifest():
    return Response(_MANIFEST_JSON, media_type="application/manifest+json",
                    headers={"Cache-Control": "public, max-age=3600"})


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    path = os.path.join(STATIC_DIR, "icons", "favicon.ico")
    return FileResponse(
        path, media_type="image/x-icon",
        headers={"Cache-Control": "public, max-age=86400"})


# Part 2's minimal service worker. Served from the ROOT (not /static/sw.js)
# deliberately: a service worker's scope is capped at the directory it's
# served from, and Part 2 needs it controlling the whole origin (/) to
# intercept navigations on every page, not just /static/*.
@app.get("/sw.js", include_in_schema=False)
async def service_worker():
    path = os.path.join(STATIC_DIR, "sw.js")
    return FileResponse(
        path, media_type="application/javascript",
        # Never cached by the browser's HTTP cache - a stale SW.js would
        # keep re-registering old logic indefinitely (the browser only
        # checks for a new SW.js on navigation/registration, and an HTTP
        # cache hit would hide a real update from even that check).
        headers={"Cache-Control": "no-cache"})


# -----------------------------------
# WEB PUSH (Part 3) - subscribe/unsubscribe/test, plus the public key the
# browser needs to create a subscription in the first place.
#
# Identity for all three POST routes below comes ONLY from the sdd_auth
# cookie (see _signed_in_email above), resolved the exact same way
# email_auth/paywall_engine resolve it - never from anything the client
# claims about itself in the request body. This is what lets these routes
# work with zero Streamlit involvement: the browser's own push-subscribe
# JS (rendered inside the app via st.components.v1.html, since it needs a
# same-origin frame with real script execution - see app.py's
# _render_push_control) calls straight back to this process with
# credentials:'include', and this process alone decides whose account the
# subscription belongs to.
# -----------------------------------

@app.get("/push/vapid-public-key", include_in_schema=False)
async def push_vapid_public_key():
    """Public by design - this is the applicationServerKey every browser's
    pushManager.subscribe() call needs, not a secret. Empty string (not an
    error) when VAPID isn't configured yet, so the UI can show "not
    available yet" instead of a broken fetch."""
    key = os.environ.get("VAPID_PUBLIC_KEY", "").strip()
    return PlainTextResponse(key, headers={"Cache-Control": "public, max-age=3600"})


@app.post("/push/subscribe", include_in_schema=False)
async def push_subscribe(request: Request):
    if not _same_origin(request):
        return Response(status_code=403)
    email = _signed_in_email(request)
    if not email:
        return Response(status_code=401)
    try:
        body = await request.json()
    except Exception:
        return Response(status_code=400)
    if not push_store.subscribe(email, body):
        return Response(status_code=400)
    return Response(status_code=204)


@app.post("/push/unsubscribe", include_in_schema=False)
async def push_unsubscribe(request: Request):
    if not _same_origin(request):
        return Response(status_code=403)
    email = _signed_in_email(request)
    if not email:
        return Response(status_code=401)
    try:
        body = await request.json()
    except Exception:
        body = {}
    endpoint = (body.get("endpoint") or "").strip()
    # Only ever unsubscribe a device that actually belongs to the caller -
    # a signed-in visitor has no business deleting anyone else's
    # subscription even if they somehow knew (or guessed) its endpoint.
    if endpoint and push_store.endpoint_owner(endpoint) == email:
        push_store.unsubscribe(endpoint)
    return Response(status_code=204)


@app.post("/push/send-test", include_in_schema=False)
async def push_send_test(request: Request):
    """Backs the admin-only "send test notification to my devices" button
    (app.py gates who ever sees that button; this route itself just
    requires sign-in and always targets the caller's OWN devices, never
    an arbitrary email, so there is no privilege check to get wrong here)."""
    if not _same_origin(request):
        return Response(status_code=403)
    email = _signed_in_email(request)
    if not email:
        return Response(status_code=401)
    if not push_send.is_configured():
        return Response(status_code=503)
    summary = push_send.send_to_email(
        email, "StocksDeepDive test notification",
        "If you can see this, push notifications are working on this device.",
        url="/",
    )
    return Response(status_code=200, content=str(summary), media_type="text/plain")


# -----------------------------------
# BLOG ROUTES  (registered before the catch-all proxy)
# -----------------------------------

def _coverage():
    """Ticker -> {industry, sections} for the Rational Compounder cards on
    the homepage, read from the same compounder_data.json the app uses.
    Read per request rather than cached at import so an admin rebuild of
    the file shows up without a restart; the file is small and the
    homepage is not hot enough for that to matter."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "compounder_data.json")
    try:
        import json
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        out = {}
        sections = data.get("sections", {}) or {}
        for tkr, meta in (data.get("tickers") or {}).items():
            n = sum(1 for s in sections.values()
                    if any((m.get("values") or {}).get(tkr) is not None
                           for m in s.get("metrics", [])))
            out[tkr] = {"industry": (meta or {}).get("industry") or "",
                        "sections": max(n, 1)}
        return out
    except Exception:
        return {}


@app.get("/", include_in_schema=False)
async def home(request: Request):
    """The indexable homepage - but ONLY for a bare "/".

    A query string containing one of _STREAMLIT_ONLY_PARAMS means this is
    not a plain visit: ?src= is an article attribution the app records,
    ?admin= is the full-view unlock, and ?code=/?state= is Google's OAuth
    callback landing back on the site. All of those have to reach
    Streamlit exactly as they did before. ?app=1 is the explicit "give me
    the live app" link on the static page, and works by the same rule.
    (Audit fix 4.3: this used to gate on ANY query string at all, which
    also sent harmless marketing/UTM links to the slow JS shell - see
    _needs_streamlit()'s comment above.)"""
    if _needs_streamlit(request) or not _renders_html("/"):
        return await _proxy(request)
    _count_view("home")
    return _html(blog_render.render_home(
        _base_url(request),
        posts=blog_store.list_posts(limit=3),
        coverage=_coverage(),
    ))


@app.get("/robots.txt", include_in_schema=False)
async def robots(request: Request):
    return PlainTextResponse(
        blog_render.render_robots(_base_url(request)),
        headers={"Cache-Control": "public, max-age=3600"},
    )


@app.get("/llms.txt", include_in_schema=False)
async def llms_txt(request: Request):
    """AI-readiness roadmap Phase 4 (citation helpers) - see
    blog_render.render_llms_txt()'s own docstring."""
    return PlainTextResponse(
        blog_render.render_llms_txt(_base_url(request)),
        headers={"Cache-Control": "public, max-age=3600"},
    )


def _snapshot_sitemap_urls(base_url):
    """<url> entries for every /s/<ticker> snapshot page, spliced into the
    existing sitemap XML below rather than changing blog_render.py's
    render_sitemap() signature - keeps that function (and every other
    caller of it) exactly as it was. AI-readiness roadmap Phase 1."""
    from xml.sax.saxutils import escape as xml_escape
    rows = snapshot_store.all_snapshots()
    urls = [
        f"  <url><loc>{xml_escape(base_url)}/s/</loc>"
        f"<changefreq>daily</changefreq><priority>0.6</priority></url>"
    ]
    for r in rows:
        lastmod = (r.get("generated_at") or "")[:10]
        urls.append(
            f"  <url><loc>{xml_escape(base_url)}/s/{xml_escape(r['ticker'])}</loc>"
            + (f"<lastmod>{lastmod}</lastmod>" if lastmod else "")
            + "<changefreq>daily</changefreq><priority>0.5</priority></url>"
        )
    return urls


@app.get("/sitemap.xml", include_in_schema=False)
async def sitemap(request: Request):
    base = _base_url(request)
    xml = blog_render.render_sitemap(blog_store.list_posts(), base,
                                     renders_html=_renders_html)
    extra = "\n".join(_snapshot_sitemap_urls(base))
    # AI-readiness roadmap Phase 4: /track-record has no Streamlit
    # equivalent (always served as real HTML, same as /s/*) so it goes in
    # here rather than through blog_render.APP_PATHS's renders_html() gate,
    # exactly like the snapshot URLs just above.
    from xml.sax.saxutils import escape as _xml_escape
    extra += (f'\n  <url><loc>{_xml_escape(base)}/track-record</loc>'
              f'<changefreq>daily</changefreq><priority>0.4</priority></url>')
    xml = xml.replace("</urlset>", extra + "\n</urlset>\n")
    return Response(xml, media_type="application/xml",
                    headers={"Cache-Control": "public, max-age=900"})


@app.get("/feed.xml", include_in_schema=False)
async def feed_alias():
    return RedirectResponse("/blog/feed.xml", status_code=301)


@app.get("/blog/feed.xml", include_in_schema=False)
async def feed(request: Request):
    xml = blog_render.render_feed(blog_store.list_posts(), _base_url(request))
    return Response(xml, media_type="application/rss+xml",
                    headers={"Cache-Control": "public, max-age=900"})


@app.get("/blog", include_in_schema=False)
async def blog_index(request: Request):
    tag = (request.query_params.get("tag") or "").strip().lower() or None
    _count_view("blog")
    posts = blog_store.list_posts(tag=tag)
    html_out = blog_render.render_index(
        posts, _base_url(request), tag=tag,
        page_title=(f"Posts tagged “{tag}” | StocksDeepDive"
                    if tag else None),
        # Tag pages are thin, near-duplicate listings of the main index -
        # crawled for their links, kept out of the index itself.
        noindex=bool(tag),
    )
    return _html(html_out)


@app.get("/blog/", include_in_schema=False)
async def blog_index_slash():
    return RedirectResponse("/blog", status_code=301)


@app.get("/blog/media/{name}", include_in_schema=False)
async def blog_media(name: str):
    path = blog_store.media_path(name)
    if not path:
        return Response("Not found", status_code=404)
    return FileResponse(
        path, headers={"Cache-Control": "public, max-age=31536000, immutable"})


@app.get("/blog/{slug}", include_in_schema=False)
async def blog_post(slug: str, request: Request):
    slug = slug.strip().lower().rstrip("/")
    base = _base_url(request)

    post = blog_store.get_post(slug)
    if not post:
        # A renamed post keeps its old address working, permanently, so an
        # indexed URL or a shared link never dies.
        target = blog_store.resolve_redirect(slug)
        if target:
            return RedirectResponse(f"/blog/{target}", status_code=301)
        draft = blog_store.get_post(slug, include_drafts=True)
        if draft and _admin_preview_ok(request):
            return _html(blog_render.render_post(draft, base),
                         cache="no-store")
        return _html(blog_render.render_not_found(base), status=404,
                     cache="no-store")

    _count_view(f"blog:{slug}")
    published = blog_store.list_posts()
    idx = next((i for i, p in enumerate(published) if p["slug"] == slug), None)
    newer = published[idx - 1] if idx not in (None, 0) else None
    older = (published[idx + 1] if idx is not None
             and idx + 1 < len(published) else None)
    comments = blog_comments_store.approved_for(slug)
    comment_status = request.query_params.get("comment")
    comment_msg = request.query_params.get("msg")
    html_out = blog_render.render_post(
        post, base, prev_post=older, next_post=newer, comments=comments,
        comment_status=comment_status, comment_msg=comment_msg,
    )
    # A page carrying a one-time "thanks"/error banner from a just-submitted
    # comment must never be cached and handed to the next visitor.
    cache = "no-store" if comment_status else "public, max-age=300"
    return _html(html_out, cache=cache)


@app.post("/blog/{slug}/comments", include_in_schema=False)
async def blog_comment_submit(slug: str, request: Request):
    """Moderated comment submission (P2.2): a plain HTML form POST, no JS
    required. Same CSRF guard as the auth-cookie endpoints - a normal
    browser form submit from the post page itself sends a Referer header
    naming this host, which is exactly what _same_origin() checks for."""
    if not _same_origin(request):
        return Response(status_code=403)
    slug = slug.strip().lower().rstrip("/")
    post = blog_store.get_post(slug)
    if not post:
        return Response(status_code=404)
    try:
        form = await request.form()
    except Exception:
        form = {}
    name = str(form.get("name") or "").strip()
    body = str(form.get("body") or "").strip()
    honeypot = str(form.get("website") or "").strip()
    ip_hash = blog_comments_store.hash_ip(_client_ip(request))
    result = blog_comments_store.add_comment(slug, name, body, ip_hash,
                                             honeypot=honeypot)
    if result["ok"]:
        target = f"/blog/{slug}?comment=thanks#comments"
    else:
        from urllib.parse import quote
        msg = quote(result.get("reason") or "Something went wrong.")
        target = f"/blog/{slug}?comment=error&msg={msg}#comments"
    return RedirectResponse(target, status_code=303)


# -----------------------------------
# CONTENT PAGES - served as HTML instead of proxied to Streamlit
#
# These three are pure prose with no interactivity, so serving them here
# costs the visitor nothing (they load instantly instead of booting a
# Streamlit session) and gains the site three genuinely indexable pages.
# The words come from site_content.py - the same source app.py renders -
# so the crawled page and the in-app page can never disagree.
# -----------------------------------

_FACTUAL = (os.environ.get("FACTUAL_MODE", "true").strip().lower()
            not in ("false", "0", "no", "off"))

_CONTENT_PAGES = {
    "/methodology": {
        "title": "How the scores work",
        "description": ("How StocksDeepDive computes intrinsic value, quality, "
                        "crowd psychology and discovery for any ASX or US "
                        "stock - every input, weight and assumption stated."),
        "page": "methodology",
    },
    "/about": {
        "title": "About",
        "description": ("StocksDeepDive is built and run by Andres Moreno, a "
                        "private investor in Australia - what the site is, and "
                        "the two principles it was built on."),
        "page": "about",
    },
    "/privacy": {
        "title": "Privacy policy",
        "description": ("What StocksDeepDive collects, what it never does, and "
                        "how to have your data deleted. No ad trackers, no "
                        "third-party analytics, nothing sold."),
        "page": "privacy",
    },
}


def _content_markdown(path):
    if path == "/methodology":
        return site_content.methodology_md(_FACTUAL)
    if path == "/about":
        return site_content.about_md(_FACTUAL)
    return site_content.PRIVACY_MD


@app.get("/methodology", include_in_schema=False)
@app.get("/about", include_in_schema=False)
@app.get("/privacy", include_in_schema=False)
async def content_page(request: Request):
    path = request.url.path.rstrip("/") or "/"
    spec = _CONTENT_PAGES.get(path)
    if not spec or not _renders_html(path):
        return await _proxy(request)
    _count_view(spec["page"])
    note = (site_content.METHODOLOGY_FACTUAL_NOTE
            if path == "/methodology" and _FACTUAL else None)
    return _html(blog_render.render_content_page(
        title=spec["title"],
        markdown_text=_content_markdown(path),
        description=spec["description"],
        path=path,
        base_url=_base_url(request),
        intro_note=note,
    ))


@app.get("/deep-dive", include_in_schema=False)
@app.get("/comparison", include_in_schema=False)
@app.get("/scanner", include_in_schema=False)
@app.get("/research", include_in_schema=False)
async def tool_landing(request: Request):
    """Same rule as the homepage: a BARE tool URL is the tool's front door
    and is served as an indexable page describing it; the instant the URL
    carries one of _STREAMLIT_ONLY_PARAMS (?ticker=, ?tickers=, ?app=1,
    ?src=, ?admin=, ?code=, ?state=) the request is a real use of the tool
    and goes straight to Streamlit. (Audit fix 4.3: previously gated on
    ANY query string at all, including harmless UTM/marketing params -
    see _needs_streamlit()'s comment above.)

    In-app navigation is unaffected - Streamlit switches pages inside the
    browser without a round trip, so these routes only ever see a fresh
    page load."""
    path = request.url.path.rstrip("/") or "/"
    if (_needs_streamlit(request) or path not in blog_render.TOOL_PAGES
            or not _renders_html(path)):
        return await _proxy(request)
    _count_view(path.lstrip("/"))
    return _html(blog_render.render_tool_landing(
        path, _base_url(request),
        coverage=_coverage() if path == "/research" else None,
    ))


# -----------------------------------
# AI-readiness roadmap Phase 1 (AI_ROADMAP_stocksdeepdive.md): public
# snapshot pages + read-only JSON API. No AI key anywhere below; every
# byte served here already appears on the public Scanner/Deep Dive
# pages, built by the nightly scan (see scheduler_engine.py /
# snapshot_store.py). /api/v1/* is a separate FastAPI sub-app (api_v1.py)
# mounted below the routes, so it can carry its own CORS policy and its
# own /openapi.json without touching this app's docs_url=None.
# -----------------------------------

_TICKER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.\-]{0,14}$")


@app.get("/s/", include_in_schema=False)
async def snapshot_index(request: Request):
    _count_view("snapshot_index")
    rows = snapshot_store.all_snapshots()
    return _html(snapshot_render.render_index(rows, _base_url(request)),
                cache="public, max-age=900")


@app.get("/s/{ticker}", include_in_schema=False)
async def snapshot_page(ticker: str, request: Request):
    ticker = ticker.strip().upper()
    base = _base_url(request)
    if not _TICKER_RE.match(ticker):
        return _html(snapshot_render.render_snapshot_not_found(base, ticker),
                    status=404, cache="no-store")
    snap = snapshot_store.get_snapshot(ticker)
    if not snap:
        return _html(snapshot_render.render_snapshot_not_found(base, ticker),
                    status=404, cache="no-store")
    _count_view("snapshot", ticker=ticker)
    return _html(snapshot_render.render_snapshot(snap, base),
                cache="public, max-age=1800")


# -----------------------------------
# AI-readiness roadmap Phase 4 (AI_ROADMAP_stocksdeepdive.md): citation
# helpers. /track-record has no Streamlit equivalent (see
# track_record_render.py's own docstring for what it is and, just as
# importantly, what it deliberately is not) so - like /s/* above - it is
# always served as real HTML, with no INDEXABLE_PAGES/_renders_html gate.
# -----------------------------------

@app.get("/track-record", include_in_schema=False)
async def track_record(request: Request):
    _count_view("track_record")
    rows = score_history.tracked_summary()
    return _html(
        track_record_render.render_track_record(rows, _base_url(request)),
        cache="public, max-age=1800",
    )


_API_DOCS_TITLE = f"API | {blog_render.SITE_NAME}"
_API_DOCS_DESC = (
    "Read-only, free, public JSON API for StocksDeepDive's computed stock "
    "scores - value, quality, psychology, discovery and moat. No API key, "
    "GET only, rate-limited, attribution requested.")


@app.get("/api", include_in_schema=False)
async def api_docs(request: Request):
    """Human-readable docs for /api/v1/* (interactive Swagger UI lives at
    /api/v1/docs, generated straight from api_v1.py). A plain prose page
    here - example requests, the universe slugs, the attribution ask -
    is what makes the API discoverable and citable by both people and
    the AI systems the roadmap is aimed at."""
    base = _base_url(request)
    universes = ", ".join(f"<code>{s}</code>" for s in
                          sorted(api_v1._SLUG_TO_UNIVERSE))
    markdown_text = f"""
StocksDeepDive's stock scores are also available as a small, free, read-only
JSON API - no key, no sign-in, GET requests only. Every response includes
`attribution`, `disclaimer` and `as_of`/`link` fields; please attribute and
link back to the site when you use this data.

**Rate limit:** 60 requests/minute per IP. **CORS:** open to any origin, GET
only. **Interactive docs (Swagger):** [{base}/api/v1/docs]({base}/api/v1/docs) -
machine-readable schema at [{base}/api/v1/openapi.json]({base}/api/v1/openapi.json).

### Endpoints

- `GET /api/v1/deep-dive/{{ticker}}` - computed scores for one ticker, e.g.
  [{base}/api/v1/deep-dive/CSL.AX]({base}/api/v1/deep-dive/CSL.AX)
- `GET /api/v1/compare?tickers=A,B,C` - up to 10 tickers side by side, e.g.
  [{base}/api/v1/compare?tickers=CSL.AX,BHP.AX]({base}/api/v1/compare?tickers=CSL.AX,BHP.AX)
- `GET /api/v1/scan/{{universe}}` - the ranked overnight scan for a whole
  index, e.g. [{base}/api/v1/scan/asx-200]({base}/api/v1/scan/asx-200).
  Universe slugs: {universes}

A ticker only has data once it has been through a nightly scan - if you get
a 404, it may not be in a covered universe yet. Every scored ticker also has
a plain HTML page at `/s/<ticker>` (e.g. [{base}/s/CSL.AX]({base}/s/CSL.AX)),
and the whole covered list is at [{base}/s/]({base}/s/).

### Using this in an AI assistant

Prefer structured tool access over scraping the API by hand? See
[{base}/ai]({base}/ai) - an MCP server exposing this same data as callable
tools for Claude, ChatGPT and other MCP-compatible assistants.

### Terms

Free for any use, commercial or not, with attribution and a link back to
stocksdeepdive.com. Nothing in this API is financial advice - see the
disclaimer in every response. No user data (portfolios, watchlists, emails)
is ever served here or ever will be.
"""
    return _html(blog_render.render_content_page(
        title=_API_DOCS_TITLE,
        markdown_text=markdown_text,
        description=_API_DOCS_DESC,
        path="/api",
        base_url=base,
        heading="API",
    ), cache="public, max-age=1800")


app.mount("/api/v1", api_v1.api_app)


# -----------------------------------
# AI-readiness roadmap Phase 2 (AI_ROADMAP_stocksdeepdive.md): an MCP
# server over the same data as /api/v1/*, for AI assistants that speak
# MCP instead of raw HTTP. No AI key anywhere here either - see
# mcp_server.py's own docstring. /ai is linked from /api only (not the
# site nav/footer): it's aimed at assistants and the people configuring
# them, not casual visitors.
# -----------------------------------

_AI_DOCS_TITLE = f"AI / MCP | {blog_render.SITE_NAME}"
_AI_DOCS_DESC = (
    "MCP server for AI assistants: deep_dive, compare, scan, moat and "
    "research_notes tools over StocksDeepDive's public computed stock "
    "scores. No API key, no auth, Streamable HTTP at /mcp.")


@app.get("/ai", include_in_schema=False)
async def ai_docs(request: Request):
    """Human-readable docs for the MCP server mounted at /mcp (see
    mcp_server.py). Linked from /api's "Using this in an AI assistant"
    section - deliberately not from the main nav or footer, since this
    page is aimed at assistants and the people configuring MCP clients,
    not casual visitors."""
    base = _base_url(request)
    markdown_text = f"""
StocksDeepDive runs an [MCP](https://modelcontextprotocol.io) (Model Context
Protocol) server - the same read-only, public stock-score data as
[the JSON API]({base}/api), exposed as callable tools for AI assistants like
Claude and ChatGPT instead of raw HTTP endpoints. No API key, no sign-in,
nothing here is user data.

**Endpoint:** `{base}/mcp` (Streamable HTTP transport).

### Tools

- **deep_dive(ticker)** - computed value/quality/psychology/discovery/moat
  scores for one stock.
- **compare(tickers)** - the same scores for up to 10 stocks side by side.
- **scan(universe, top_n)** - the ranked overnight scan for a whole index
  (e.g. "ASX 200", "S&P 500"), highest score first.
- **moat(ticker)** - the cached Moat Score and erosion flag for one stock.
- **research_notes(ticker)** - any StocksDeepDive research notes written
  about that company, with links.

Every tool result includes `attribution`, `disclaimer` and `as_of`/`link`
fields, same as the JSON API - please attribute and link back to the site
when you surface this data to someone.

### Connecting a client

Most MCP clients (Claude Desktop, Claude Code, and others that support
Streamable HTTP) accept a server URL directly. Point yours at:

```
{base}/mcp
```

No headers, no authentication, no configuration beyond the URL.

### Prefer just reading pages?

[{base}/llms.txt]({base}/llms.txt) is a plain-Markdown index of the
site's worthwhile pages, meant to be fetched directly instead of
crawling the whole site - a lighter option than either the API or MCP
when all an assistant needs is to read, not call tools.

### Terms

Free for any use, commercial or not, with attribution and a link back to
stocksdeepdive.com. Nothing served here is financial advice - see the
disclaimer in every tool result. No user data (portfolios, watchlists,
emails) is ever served here or ever will be.
"""
    return _html(blog_render.render_content_page(
        title=_AI_DOCS_TITLE,
        markdown_text=markdown_text,
        description=_AI_DOCS_DESC,
        path="/ai",
        base_url=base,
        heading="AI / MCP",
    ), cache="public, max-age=1800")


@app.api_route("/mcp", methods=["GET", "POST", "DELETE"], include_in_schema=False)
async def mcp_bare_path_redirect():
    """Starlette's Mount("/mcp", ...) below only matches "/mcp/..." (a
    trailing slash or deeper), never the bare "/mcp" path itself - and
    because this app also registers a catch-all proxy route (further
    down, for Streamlit), an unmatched bare "/mcp" would silently fall
    through to THAT instead of ever reaching the MCP sub-app or getting
    Starlette's usual redirect-slash handling. Nearly every MCP client
    (including the official Python SDK, which always sets
    follow_redirects=True) is given a server URL without a trailing
    slash, so this exists purely to make plain {base}/mcp - the URL the
    /ai docs page and every client config actually uses - work. 307
    preserves the original method and body, so POST's JSON-RPC payload
    survives the redirect intact."""
    return RedirectResponse(url="/mcp/", status_code=307)


app.mount("/mcp", mcp_server.mcp.streamable_http_app())


@app.get("/{token}.html", include_in_schema=False)
async def gsc_verification(token: str, request: Request):
    """Google Search Console's HTML-file verification. The app's own pages
    can't carry a verification meta tag (Streamlit renders no <head> we
    control), so file verification is the practical route - set
    GOOGLE_SITE_VERIFICATION_FILE to the filename Google gives you."""
    want = os.environ.get("GOOGLE_SITE_VERIFICATION_FILE", "").strip()
    if want and want == f"{token}.html":
        return PlainTextResponse(f"google-site-verification: {want}")
    return await _proxy(request)


# -----------------------------------
# REVERSE PROXY - everything that is not the blog
# -----------------------------------

async def _proxy(request: Request):
    """Stream a request through to Streamlit and stream the response back.

    Streaming both ways matters: Streamlit's own /_stcore endpoints and
    static assets can be large, and buffering them in memory would make
    the proxy the slowest part of the app."""
    assert _client is not None
    url = httpx.URL(path=request.url.path, query=request.url.query.encode())

    headers = [(k, v) for k, v in request.headers.raw
               if k.decode().lower() not in HOP_BY_HOP]
    req = _client.build_request(
        request.method, url, headers=headers,
        content=request.stream(),
    )
    try:
        upstream = await _client.send(req, stream=True)
    except httpx.ConnectError:
        return HTMLResponse(
            "<h1>Starting up</h1><p>The app is still starting. "
            "Please refresh in a few seconds.</p>"
            '<p><a href="/blog">Read the blog</a> in the meantime.</p>',
            status_code=503, headers={"Retry-After": "10"},
        )
    except httpx.RequestError as exc:
        log.warning("upstream error for %s: %s", request.url.path, exc)
        return Response("Bad gateway", status_code=502)

    # content-length is dropped because the body is re-chunked on the way
    # out; date and server are dropped because uvicorn writes its own and
    # two of each is a malformed response.
    _drop = HOP_BY_HOP | {"content-length", "date", "server"}
    resp_headers = [(k, v) for k, v in upstream.headers.raw
                    if k.decode().lower() not in _drop]

    # Audit fix 4.1: everything that reaches this function is proxied
    # straight through to the Streamlit JS shell - no server-rendered
    # <head>, so nothing anywhere in this codebase can otherwise mark
    # these noindex (no meta tag is possible on a page that's just a JS
    # bootstrap div). Every ?ticker=/?tickers= URL on /deep-dive,
    # /comparison, /scanner, /research lands here, and the server-rendered
    # homepage/research page both link crawlers into exactly these URLs
    # via ticker chips and the research coverage grid - without this
    # header, robots.txt's default-allow lets a crawler index a large
    # footprint of near-identical empty-shell pages, which can drag down
    # ranking for the pages that ARE real content. The response headers
    # here are already fully rewritable (see the content-length/date/
    # server stripping just above), so this needs no HTML changes and
    # applies uniformly to every proxied route, curated server-rendered
    # routes never reach _proxy() at all.
    resp_headers.append((b"x-robots-tag", b"noindex"))

    content_type = (upstream.headers.get("content-type") or "").lower()

    # PWA head-tag injection (Part 1c): the Streamlit shell is the ONLY
    # thing this proxy ever serves with content-type text/html - every
    # other proxied response (the /_stcore JS/CSS bundles, images, JSON
    # API calls) is some other type and falls straight through to the
    # streaming path below, completely untouched. Gated on content-type,
    # never on URL, per the brief: guessing by path (e.g. "/" or
    # "?ticker=") would silently miss a route this file doesn't know
    # about yet, where matching the actual header can't.
    #
    # The shell is a few KB (see Streamlit's static/index.html) - reading
    # it fully into memory here costs nothing, unlike buffering a large
    # asset or a long-lived /_stcore stream would.
    if content_type.startswith("text/html"):
        html_bytes = await upstream.aread()
        await upstream.aclose()
        html_bytes = _inject_pwa_head_tags(html_bytes)
        # aread() already transparently decompressed the body (httpx
        # decodes Content-Encoding for a normal .aread()/.content read,
        # unlike the raw passthrough in aiter_raw() below) - forwarding
        # the original Content-Encoding header here would tell the
        # browser to gunzip bytes that are no longer gzipped.
        html_headers = [(k, v) for k, v in resp_headers
                        if k.decode().lower() != "content-encoding"]
        html_headers.append((b"content-length", str(len(html_bytes)).encode()))
        response = HTMLResponse(html_bytes, status_code=upstream.status_code)
        response.raw_headers = html_headers
        return response

    async def body():
        try:
            async for chunk in upstream.aiter_raw():
                yield chunk
        finally:
            await upstream.aclose()

    response = StreamingResponse(body(), status_code=upstream.status_code)
    # raw_headers rather than a dict: a dict would collapse repeated
    # headers, and Set-Cookie legitimately repeats.
    response.raw_headers = resp_headers
    return response


_VIEWPORT_RE = re.compile(r'<meta\s+name="viewport"[^>]*/?>',
                          re.IGNORECASE | re.DOTALL)


def _inject_pwa_head_tags(html_bytes: bytes) -> bytes:
    """Insert the manifest/icon/PWA meta tags and the service-worker
    registration snippet into the proxied Streamlit shell's own <head>,
    and add viewport-fit=cover to its existing viewport tag so the
    safe-area padding in app.py's top bar means something in standalone
    (installed) mode. blog_render.PWA_HEAD_TAGS is the SAME constant the
    server-rendered pages use (see blog_render._head()) - one source for
    both injection points, so they can't drift apart.

    Never raises on unexpected shell markup: a decode failure or a
    missing </head> just returns the original bytes untouched rather than
    risking a mangled page - a missing PWA tag is a MUCH smaller problem
    than a broken app shell."""
    try:
        text = html_bytes.decode("utf-8")
    except UnicodeDecodeError:
        return html_bytes

    def _add_viewport_fit(match):
        tag = match.group(0)
        if "viewport-fit" in tag:
            return tag
        return re.sub(r'content="([^"]*)"', r'content="\1, viewport-fit=cover"',
                      tag, count=1)

    text = _VIEWPORT_RE.sub(_add_viewport_fit, text, count=1)

    if "</head>" in text:
        text = text.replace("</head>", blog_render.PWA_HEAD_TAGS + "\n</head>", 1)
    return text.encode("utf-8")


@app.api_route("/{path:path}", include_in_schema=False,
               methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD",
                        "OPTIONS"])
async def catch_all(path: str, request: Request):
    return await _proxy(request)


# -----------------------------------
# WEBSOCKET PROXY  (/_stcore/stream - the app's entire lifeline)
# -----------------------------------

def _ws_connect():
    """websockets moved its client between versions; support both so a
    routine `pip install -U` can't break the site."""
    try:
        from websockets.asyncio.client import connect  # websockets >= 13
        return connect, "additional_headers"
    except Exception:
        from websockets.legacy.client import connect  # websockets < 13
        return connect, "extra_headers"


@app.websocket("/{path:path}")
async def ws_proxy(ws: WebSocket, path: str):
    connect, header_kw = _ws_connect()

    headers = {}
    for k, v in ws.scope.get("headers", []):
        key = k.decode().lower()
        # Host and Origin are forwarded deliberately: Streamlit accepts a
        # websocket when Origin matches Host, and the browser's real pair
        # matches. The handshake-specific headers are rebuilt by the client
        # library and must not be copied.
        if key in HOP_BY_HOP or key.startswith("sec-websocket") or key == "host":
            continue
        headers[k.decode()] = v.decode()

    query = ws.scope.get("query_string", b"").decode()
    public_host = None
    for k, v in ws.scope.get("headers", []):
        if k.decode().lower() == "host":
            public_host = v.decode()
            break
    # Keep the public host in the URI (so the upstream Host header is the
    # browser's), but point the actual TCP connection at loopback.
    uri = f"ws://{public_host or STREAMLIT_HOST}/{path}" + (f"?{query}" if query else "")
    subprotocols = ws.scope.get("subprotocols") or []

    kwargs = {
        header_kw: headers,
        "host": STREAMLIT_HOST,
        "port": STREAMLIT_PORT,
        "open_timeout": 20,
        "max_size": None,
        "ping_interval": 20,
        "ping_timeout": 60,
    }
    if subprotocols:
        kwargs["subprotocols"] = subprotocols

    try:
        upstream = await connect(uri, **kwargs)
    except TypeError:
        # Older/newer signature without host/port overrides - fall back to
        # addressing loopback directly.
        kwargs.pop("host", None)
        kwargs.pop("port", None)
        uri = f"ws://{STREAMLIT_HOST}:{STREAMLIT_PORT}/{path}" + (
            f"?{query}" if query else "")
        upstream = await connect(uri, **kwargs)
    except Exception as exc:
        log.warning("websocket upstream refused (%s): %s", path, exc)
        await ws.close(code=1013)  # try again later
        return

    negotiated = getattr(upstream, "subprotocol", None)
    await ws.accept(subprotocol=negotiated)

    async def browser_to_app():
        try:
            while True:
                msg = await ws.receive()
                if msg["type"] == "websocket.disconnect":
                    break
                if msg.get("text") is not None:
                    await upstream.send(msg["text"])
                elif msg.get("bytes") is not None:
                    await upstream.send(msg["bytes"])
        except Exception:
            pass
        finally:
            with suppress(Exception):
                await upstream.close()

    async def app_to_browser():
        try:
            async for message in upstream:
                if isinstance(message, bytes):
                    await ws.send_bytes(message)
                else:
                    await ws.send_text(message)
        except Exception:
            pass
        finally:
            with suppress(Exception):
                await ws.close()

    # Audit fix 4.4: asyncio.gather() joins both directions, but if one
    # side finishes (its finally block above closes the shared ws/upstream
    # resource) while the OTHER side is blocked on a stale/unresponsive
    # connection (closed laptop, dead network) rather than a clean
    # disconnect event, closing the resource from this side doesn't
    # reliably unblock a read that's stuck at the transport level - gather
    # then just waits forever for a task that will never finish, and this
    # accumulates over the normal churn of Streamlit session recycling.
    # asyncio.wait(..., FIRST_COMPLETED) + explicit cancellation of
    # whichever task is still pending is the standard bidirectional-proxy
    # fix: once either direction ends for any reason, the other is force-
    # cancelled rather than trusted to notice on its own.
    task_b2a = asyncio.create_task(browser_to_app())
    task_a2b = asyncio.create_task(app_to_browser())
    _, pending = await asyncio.wait(
        {task_b2a, task_a2b}, return_when=asyncio.FIRST_COMPLETED)
    for t in pending:
        t.cancel()
        with suppress(Exception):
            await t


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info",
                ws_max_size=None, timeout_keep_alive=65,
                # Railway terminates TLS in front of us and passes the real
                # scheme/IP in X-Forwarded-*.
                proxy_headers=True, forwarded_allow_ips="*")
