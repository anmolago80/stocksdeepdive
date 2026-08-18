# The blog — how it works and how to go live

## Why it isn't just another Streamlit page

Streamlit sends the browser a JavaScript shell and streams every word of
content over a websocket. A search crawler fetching `/research` gets an empty
shell, one generic `<title>`, and no article text — and there's no way to serve
`/robots.txt` or `/sitemap.xml` from inside Streamlit at all. **No Streamlit
page on this site can meaningfully be indexed.**

So the blog is served *in front of* Streamlit, as real HTML:

```
browser ──► server.py (on $PORT)
              ├─ /blog, /blog/<slug>, /blog/media/…    ← real HTML, rendered here
              ├─ /sitemap.xml, /robots.txt, /blog/feed.xml
              └─ everything else ───────────────────────► Streamlit on 127.0.0.1:8501
                                                           (HTTP + websocket, proxied)
```

The app is untouched on every non-blog URL. Posts live on the main domain
(`stocksdeepdive.com/blog/<slug>`), not a subdomain, so the links they earn
count towards the domain the tools live on.

## Files

| File | What it does |
|---|---|
| `server.py` | The entrypoint. Serves the blog, proxies everything else. |
| `blog_store.py` | SQLite storage (same `stocksdeepdive.db` on the Railway volume as every other store) + hero images in `blog_media/`. |
| `blog_render.py` | Markdown → HTML, the page templates, and the SEO head: title, meta description, canonical, Open Graph, JSON-LD `BlogPosting`, sitemap, RSS. |
| `app.py` | New admin page `page_blog_admin` at `/blog-admin`, plus a Blog link in the footer. |

## Going live on Railway

Change the service's **start command** from

```
streamlit run app.py --server.port $PORT --server.address 0.0.0.0 --server.headless true --browser.gatherUsageStats false
```

to

```
python server.py
```

Nothing else changes: `server.py` starts Streamlit itself, on an internal port,
with the same flags. Rolling back is the same one-line edit in reverse.

`requirements.txt` gains `fastapi`, `uvicorn[standard]`, `httpx`, `websockets`
and `markdown`.

### Optional environment variables

| Variable | Effect |
|---|---|
| `PUBLIC_BASE_URL` | Canonical origin written into canonical tags, the sitemap and the feed. Defaults to `https://stocksdeepdive.com`. |
| `GOOGLE_SITE_VERIFICATION_FILE` | e.g. `google1a2b3c.html` — served for Search Console's HTML-file verification. |
| `GOOGLE_SITE_VERIFICATION_TAG` | Alternative: the meta-tag content value (appears on blog pages). |
| `DEFAULT_OG_IMAGE` | Absolute URL used as the social card image for posts with no hero image. |
| `STREAMLIT_INTERNAL_PORT` | Where Streamlit is run internally. Default `8501`. |

## Writing a post

Open `/blog-admin` (or unlock full view first with `?admin=<ADMIN_REFRESH_KEY>`
and go to `/blog-admin`). The editor takes a title, a URL slug, a meta
description, Markdown body, tags, author, an optional hero image, and a
Draft/Published toggle.

- **The slug is the URL** and is the most permanent thing about a post. Renaming
  a published post's slug leaves a permanent 301 behind, so an indexed or shared
  link never breaks.
- **Drafts** are hidden from the blog index, the sitemap and the feed, and carry
  `noindex`. They are still viewable at their own URL with
  `?preview=<ADMIN_REFRESH_KEY>`.
- **Meta description** is what Google prints under the title. Left blank, the
  opening of the post is used instead.
- Posts and images live on the Railway volume, so they survive redeploys.

## After the first deploy

1. Visit `https://stocksdeepdive.com/sitemap.xml` and `/robots.txt` to confirm
   they answer.
2. Add the property in Google Search Console (DNS TXT is easiest; otherwise set
   `GOOGLE_SITE_VERIFICATION_FILE`), then submit the sitemap.
3. Publish posts that answer questions people actually type. The tools can't be
   indexed; the blog is the front door, and every post links back into them.
