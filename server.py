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
import logging
import os
import signal
import subprocess
import sys
import time
from contextlib import asynccontextmanager, suppress
from urllib.parse import urlparse

import httpx
from fastapi import FastAPI, Request, Response, WebSocket
from fastapi.responses import (FileResponse, HTMLResponse, PlainTextResponse,
                               RedirectResponse, StreamingResponse)

import blog_render
import blog_store
import site_content

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
    _client = httpx.AsyncClient(
        base_url=UPSTREAM, timeout=httpx.Timeout(None, connect=10.0),
        follow_redirects=False, limits=httpx.Limits(max_connections=200),
    )
    _streamlit_proc = _start_streamlit()
    # Don't block startup on it: /blog must answer even while the app is
    # still importing, and Railway's own health probe shouldn't time out.
    asyncio.create_task(_wait_for_streamlit())
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


def _html(content, status=200, cache="public, max-age=300"):
    return HTMLResponse(content, status_code=status,
                        headers={"Cache-Control": cache})


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


def _admin_preview_ok(request: Request) -> bool:
    """Draft posts are viewable at their real URL only by the admin - the
    same ADMIN_REFRESH_KEY the app uses, passed as ?preview=<key>. Drafts
    are noindex, absent from the index, sitemap and feed."""
    key = os.environ.get("ADMIN_REFRESH_KEY", "").strip()
    if not key:
        return False
    return (request.query_params.get("preview") or "").strip() == key


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

    Any query string means this is not a plain visit: ?src= is an article
    attribution the app records, ?admin= is the full-view unlock, and
    ?code=/?state= is Google's OAuth callback landing back on the site.
    All of those have to reach Streamlit exactly as they did before, so
    anything with a query is proxied untouched. ?app=1 is the explicit
    "give me the live app" link on the static page, and works by the same
    rule."""
    if request.url.query:
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


@app.get("/sitemap.xml", include_in_schema=False)
async def sitemap(request: Request):
    xml = blog_render.render_sitemap(blog_store.list_posts(), _base_url(request))
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
    return _html(blog_render.render_post(post, base, prev_post=older,
                                         next_post=newer))


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
    if not spec:
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
    and is served as an indexable page describing it; the instant there is
    a query on the URL (?ticker=, ?tickers=, ?app=1, ?src=, ?admin=) the
    request is a real use of the tool and goes straight to Streamlit.

    In-app navigation is unaffected - Streamlit switches pages inside the
    browser without a round trip, so these routes only ever see a fresh
    page load."""
    path = request.url.path.rstrip("/") or "/"
    if request.url.query or path not in blog_render.TOOL_PAGES:
        return await _proxy(request)
    _count_view(path.lstrip("/"))
    return _html(blog_render.render_tool_landing(
        path, _base_url(request),
        coverage=_coverage() if path == "/research" else None,
    ))


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

    await asyncio.gather(browser_to_app(), app_to_browser())


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info",
                ws_max_size=None, timeout_keep_alive=65,
                # Railway terminates TLS in front of us and passes the real
                # scheme/IP in X-Forwarded-*.
                proxy_headers=True, forwarded_allow_ips="*")
