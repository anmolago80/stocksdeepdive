"""
blog_render.py

Turns rows from blog_store into complete, server-rendered HTML pages.

This module is the whole reason the blog exists in the form it does. The
Streamlit app cannot be indexed by search engines - it ships a JavaScript
shell and streams every pixel of content over a websocket, so a crawler
fetching https://stocksdeepdive.com/research receives no article text, no
per-page <title> and no description. Everything here is the opposite: a
plain HTML document, complete in the first response, with a real title,
meta description, canonical URL, Open Graph card and JSON-LD article
schema. server.py serves these bytes directly; Streamlit is never involved
in a /blog request.

Styling is inlined rather than pulled from a stylesheet so a blog page is
a single round trip, and deliberately mirrors the app's dark theme
(.streamlit/config.toml) so a reader crossing from an article into the
tools doesn't feel like they changed sites.
"""

import html
import os
import re
from datetime import datetime, timezone
from xml.sax.saxutils import escape as xml_escape

import blog_store

SITE_NAME = "StocksDeepDive"
DEFAULT_AUTHOR = "StocksDeepDive"

# The app's own pages, listed in the sitemap alongside the posts. Crawlers
# will not get readable content from these (Streamlit again), but listing
# them is still how the URLs get discovered and how the site's shape is
# declared - and if the app ever gains a pre-rendered layer, they are
# already declared.
APP_PATHS = [
    ("/", "1.0", "daily"),
    ("/research", "0.9", "weekly"),
    ("/deep-dive", "0.8", "weekly"),
    ("/comparison", "0.6", "monthly"),
    ("/scanner", "0.6", "weekly"),
    ("/methodology", "0.7", "monthly"),
    ("/about", "0.5", "monthly"),
    ("/privacy", "0.3", "yearly"),
]

DISCLAIMER = (
    "<b>Factual information and general commentary only.</b> StocksDeepDive "
    "publishes data, model outputs and described calculations from stated "
    "inputs. Nothing on this site takes your personal objectives, financial "
    "situation or needs into account, and nothing here is financial product "
    "advice or a recommendation to buy, hold or sell any security. Model "
    "outputs depend entirely on their stated inputs and assumptions. Consider "
    "seeking advice from a licensed adviser before acting. Data via Yahoo "
    "Finance, Google Trends, StockTwits and NewsAPI; figures may be delayed "
    "or revised."
)


# -----------------------------------
# MARKDOWN
# -----------------------------------

def md_to_html(text):
    """Post body Markdown -> HTML. The markdown package is a hard
    dependency (requirements.txt), but a missing/broken install must not
    take the site down, so the fallback degrades to escaped paragraphs
    rather than raising."""
    text = text or ""
    try:
        import markdown as _md
        return _md.markdown(
            text,
            extensions=["extra", "sane_lists", "smarty", "admonition"],
            output_format="html5",
        )
    except Exception:
        parts = [html.escape(p).replace("\n", "<br>")
                 for p in re.split(r"\n\s*\n", text) if p.strip()]
        return "".join(f"<p>{p}</p>" for p in parts)


def reading_time(text):
    words = len(re.findall(r"\w+", text or ""))
    return max(1, round(words / 225))


def _plain(text, limit=None):
    """Markdown stripped back to plain prose - used to auto-fill a meta
    description when the author left one blank. A description is what
    Google shows under the title in the results page, so an approximate
    one beats none at all."""
    t = re.sub(r"```.*?```", " ", text or "", flags=re.S)
    t = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", t)
    t = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", t)
    t = re.sub(r"[#>*_`|-]+", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    if limit and len(t) > limit:
        t = t[:limit].rsplit(" ", 1)[0] + "…"
    return t


def post_description(post):
    return (post.get("summary") or "").strip() or _plain(post.get("body_md"), 155)


def _iso_date(value):
    if not value:
        return ""
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).isoformat()
    except Exception:
        return value


def _human_date(value):
    if not value:
        return ""
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt.strftime("%-d %B %Y") if os.name != "nt" else dt.strftime("%d %B %Y")
    except Exception:
        return value[:10]


def post_url(base_url, slug):
    return f"{base_url}/blog/{slug}"


def hero_url(base_url, post):
    f = post.get("hero_file")
    return f"{base_url}/blog/media/{f}" if f else None


# -----------------------------------
# PAGE SHELL
# -----------------------------------

_CSS = """
*,*::before,*::after{box-sizing:border-box}
body{margin:0;background:#0b1220;color:#e6edf5;
  font-family:'Segoe UI',system-ui,-apple-system,Roboto,Helvetica,Arial,sans-serif;
  font-size:17px;line-height:1.7;-webkit-font-smoothing:antialiased}
a{color:#2dd4bf;text-decoration:none}
a:hover{text-decoration:underline}
.wrap{max-width:760px;margin:0 auto;padding:0 22px}
header.site{border-bottom:1px solid #1f3352;padding:16px 0}
header.site .wrap{max-width:1080px;display:flex;align-items:center;gap:26px;flex-wrap:wrap}
.brand{font-size:21px;font-weight:800;color:#e6edf5;text-decoration:none}
.brand .accent{color:#2dd4bf}
nav.site{display:flex;gap:20px;flex-wrap:wrap;margin-left:auto}
nav.site a{color:#8aa0b8;font-size:14px}
nav.site a:hover{color:#e6edf5;text-decoration:none}
main{padding:34px 0 10px}
h1{font-size:40px;line-height:1.2;font-weight:800;margin:0 0 14px;letter-spacing:-.5px}
h2{font-size:26px;line-height:1.3;font-weight:700;margin:38px 0 12px}
h3{font-size:20px;font-weight:700;margin:28px 0 8px}
h4{font-size:17px;font-weight:700;margin:22px 0 6px;color:#cddaea}
p{margin:0 0 18px}
ul,ol{margin:0 0 18px;padding-left:24px}
li{margin:6px 0}
blockquote{margin:22px 0;padding:2px 0 2px 18px;border-left:3px solid #2dd4bf;color:#b9c9dc}
code{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:14.5px;
  background:#121f36;border:1px solid #1f3352;border-radius:5px;padding:1px 5px}
pre{background:#121f36;border:1px solid #1f3352;border-radius:10px;padding:16px;
  overflow:auto;margin:0 0 20px}
pre code{background:none;border:0;padding:0;font-size:14px}
img{max-width:100%;height:auto;border-radius:10px;display:block}
figure{margin:26px 0}
figcaption{color:#8aa0b8;font-size:13.5px;margin-top:8px;text-align:center}
table{width:100%;border-collapse:collapse;margin:0 0 22px;font-size:15px}
th,td{border:1px solid #1f3352;padding:9px 11px;text-align:left;vertical-align:top}
th{background:#121f36;color:#e6edf5;font-weight:700}
hr{border:0;border-top:1px solid #1f3352;margin:34px 0}
.kicker{font-family:ui-monospace,Menlo,monospace;font-size:11.5px;letter-spacing:1.6px;
  text-transform:uppercase;color:#2dd4bf;margin-bottom:12px}
.meta{color:#8aa0b8;font-size:14px;margin:0 0 26px}
.meta a{color:#8aa0b8}
.lede{font-size:19px;color:#b9c9dc;margin:0 0 26px}
.tags{margin:30px 0 0;display:flex;gap:8px;flex-wrap:wrap}
.tag{background:#121f36;border:1px solid #1f3352;border-radius:999px;
  padding:3px 12px;font-size:12.5px;color:#8aa0b8}
.card{display:block;background:#121f36;border:1px solid #1f3352;border-radius:12px;
  padding:20px 22px;margin-bottom:16px;text-decoration:none;color:inherit;
  transition:border-color .15s ease,transform .15s ease}
.card:hover{border-color:#2dd4bf;text-decoration:none;transform:translateY(-1px)}
.card h2{margin:0 0 8px;font-size:21px;color:#e6edf5}
.card p{margin:0 0 10px;color:#b9c9dc;font-size:15.5px}
.card .meta{margin:0;font-size:13px}
.cta{background:#121f36;border:1px solid #1f3352;border-left:3px solid #2dd4bf;
  border-radius:10px;padding:18px 22px;margin:40px 0 10px}
.cta h3{margin:0 0 6px;font-size:18px}
.cta p{margin:0;color:#b9c9dc;font-size:15px}
footer.site{border-top:1px solid #1f3352;margin-top:56px;padding:26px 0 34px;
  color:#8aa0b8;font-size:13px}
footer.site .wrap{max-width:1080px}
.f-cols{display:flex;gap:60px;flex-wrap:wrap;margin-bottom:18px}
.f-cols h5{color:#e6edf5;font-size:13px;margin:0 0 4px}
.f-cols a{display:block;color:#8aa0b8;margin-top:6px;font-size:13px}
.f-cols a:hover{color:#e6edf5;text-decoration:none}
.disclaimer{border-top:1px solid #1f3352;padding-top:14px;max-width:900px}
.disclaimer b{color:#8aa0b8}
.empty{color:#8aa0b8;background:#121f36;border:1px solid #1f3352;
  border-radius:12px;padding:26px;text-align:center}
@media(max-width:640px){
  body{font-size:16px}
  h1{font-size:31px}
  h2{font-size:23px}
  nav.site{gap:14px;margin-left:0;width:100%}
  .f-cols{gap:28px}
}
"""


def _head(title, description, canonical, base_url, image=None,
          noindex=False, extra_meta="", json_ld=""):
    """The part search engines actually read. Every page gets a unique
    title and description, an explicit canonical (so a URL reached with a
    tracking ?src= parameter still consolidates to one address), and an
    Open Graph card so a shared link renders as a card rather than a bare
    URL."""
    e = html.escape
    # og:image is emitted only when there is a real image to point at - a
    # card referencing a 404 renders worse on Twitter/LinkedIn than no card
    # image at all, and the summary card is the honest fallback.
    img = image or os.environ.get("DEFAULT_OG_IMAGE", "").strip() or None
    tags = [
        '<meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width,initial-scale=1">',
        f"<title>{e(title)}</title>",
        f'<meta name="description" content="{e(description)}">',
        f'<link rel="canonical" href="{e(canonical)}">',
        ('<meta name="robots" content="noindex,follow">' if noindex else
         '<meta name="robots" content="index,follow,max-image-preview:large,'
         'max-snippet:-1">'),
        f'<meta property="og:site_name" content="{e(SITE_NAME)}">',
        f'<meta property="og:title" content="{e(title)}">',
        f'<meta property="og:description" content="{e(description)}">',
        f'<meta property="og:url" content="{e(canonical)}">',
        ('<meta name="twitter:card" content="summary_large_image">' if img
         else '<meta name="twitter:card" content="summary">'),
        f'<meta name="twitter:title" content="{e(title)}">',
        f'<meta name="twitter:description" content="{e(description)}">',
        f'<link rel="alternate" type="application/rss+xml" '
        f'title="{e(SITE_NAME)} blog" href="{base_url}/blog/feed.xml">',
        '<meta name="theme-color" content="#0b1220">',
    ]
    if img:
        tags.append(f'<meta property="og:image" content="{e(img)}">')
        tags.append(f'<meta name="twitter:image" content="{e(img)}">')
    gsc = os.environ.get("GOOGLE_SITE_VERIFICATION_TAG", "").strip()
    if gsc:
        tags.append(f'<meta name="google-site-verification" content="{e(gsc)}">')
    if extra_meta:
        tags.append(extra_meta)
    tags.append(f"<style>{_CSS}</style>")
    if json_ld:
        tags.append(f'<script type="application/ld+json">{json_ld}</script>')
    return "\n".join(tags)


def _header_html():
    return """
<header class="site"><div class="wrap">
  <a class="brand" href="/">Stocks<span class="accent">DeepDive</span></a>
  <nav class="site">
    <a href="/blog">Blog</a>
    <a href="/research">Rational Compounder</a>
    <a href="/deep-dive">Deep Dive</a>
    <a href="/scanner">Scanner</a>
    <a href="/methodology">How the scores work</a>
  </nav>
</div></header>
"""


def _footer_html():
    return f"""
<footer class="site"><div class="wrap">
  <div class="f-cols">
    <div><h5>StocksDeepDive</h5>
      <a href="/">Home</a>
      <a href="/blog">Blog</a>
      <a href="/about">About the author</a>
      <a href="/methodology">How the scores work</a>
      <a href="/research">Rational Compounder Research</a>
    </div>
    <div><h5>Tools</h5>
      <a href="/deep-dive">Stock Deep Dive</a>
      <a href="/comparison">Comparison</a>
      <a href="/scanner">Stock Scanner</a>
    </div>
    <div><h5>Contact</h5>
      <a href="mailto:rationalcompounder@stocksdeepdive.com">rationalcompounder@stocksdeepdive.com</a>
      <a href="/privacy">Privacy policy</a>
      <a href="/blog/feed.xml">RSS feed</a>
    </div>
  </div>
  <div class="disclaimer">{DISCLAIMER}</div>
</div></footer>
"""


def _page(head, body):
    return (f"<!doctype html>\n<html lang=\"en\">\n<head>\n{head}\n</head>\n"
            f"<body>\n{_header_html()}\n{body}\n{_footer_html()}\n</body>\n</html>")


# -----------------------------------
# STRUCTURED DATA
#
# JSON-LD is what turns a page into a rich result: an article with a date
# and an author rather than an anonymous URL. Kept hand-built (no
# dependency) and validated against schema.org's Article requirements.
# -----------------------------------

def _json_ld(obj):
    import json
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def _post_json_ld(post, base_url):
    url = post_url(base_url, post["slug"])
    graph = {
        "@context": "https://schema.org",
        "@type": "BlogPosting",
        "headline": post["title"][:110],
        "description": post_description(post),
        "url": url,
        "mainEntityOfPage": {"@type": "WebPage", "@id": url},
        "datePublished": _iso_date(post.get("published_at")),
        "dateModified": _iso_date(post.get("updated_at")
                                  or post.get("published_at")),
        "author": {"@type": "Person",
                   "name": post.get("author") or DEFAULT_AUTHOR},
        "publisher": {
            "@type": "Organization",
            "name": SITE_NAME,
            "url": base_url,
        },
        "inLanguage": "en",
        "isAccessibleForFree": True,
    }
    img = hero_url(base_url, post)
    if img:
        graph["image"] = [img]
    kw = blog_store.tag_list(post)
    if kw:
        graph["keywords"] = ", ".join(kw)
    breadcrumb = {
        "@context": "https://schema.org",
        "@type": "BreadcrumbList",
        "itemListElement": [
            {"@type": "ListItem", "position": 1, "name": "Home",
             "item": base_url},
            {"@type": "ListItem", "position": 2, "name": "Blog",
             "item": f"{base_url}/blog"},
            {"@type": "ListItem", "position": 3, "name": post["title"],
             "item": url},
        ],
    }
    return _json_ld([graph, breadcrumb])


def _index_json_ld(posts, base_url):
    return _json_ld({
        "@context": "https://schema.org",
        "@type": "Blog",
        "name": f"{SITE_NAME} Blog",
        "url": f"{base_url}/blog",
        "description": ("Value-investing research notes, valuation walk-throughs "
                        "and method explainers from StocksDeepDive."),
        "publisher": {"@type": "Organization", "name": SITE_NAME, "url": base_url},
        "blogPost": [
            {"@type": "BlogPosting",
             "headline": p["title"][:110],
             "url": post_url(base_url, p["slug"]),
            "datePublished": _iso_date(p.get("published_at"))}
            for p in posts[:20]
        ],
    })


# -----------------------------------
# PAGES
# -----------------------------------

INDEX_TITLE = f"Blog - value investing research notes | {SITE_NAME}"
INDEX_DESC = ("Research notes, valuation walk-throughs and method explainers "
              "from StocksDeepDive - how the numbers behind each stock verdict "
              "are actually calculated.")


def render_index(posts, base_url, page_title=None, description=None,
                 tag=None, noindex=False):
    e = html.escape
    canonical = f"{base_url}/blog" + (f"?tag={tag}" if tag else "")
    title = page_title or INDEX_TITLE
    desc = description or INDEX_DESC

    cards = []
    for p in posts:
        meta_bits = []
        if p.get("published_at"):
            meta_bits.append(_human_date(p["published_at"]))
        meta_bits.append(f"{reading_time(p.get('body_md'))} min read")
        if p.get("status") != blog_store.STATUS_PUBLISHED:
            meta_bits.append("DRAFT")
        cards.append(f"""
  <a class="card" href="/blog/{e(p['slug'])}">
    <h2>{e(p['title'])}</h2>
    <p>{e(post_description(p))}</p>
    <div class="meta">{e(' · '.join(meta_bits))}</div>
  </a>""")

    if not cards:
        cards.append('<div class="empty">No posts published yet - '
                     'the first one is on its way.</div>')

    tag_links = ""
    all_tags = blog_store.all_tags()
    if all_tags:
        links = ['<a class="tag" href="/blog">All</a>'] + [
            f'<a class="tag" href="/blog?tag={e(t)}">{e(t)} ({n})</a>'
            for t, n in all_tags[:12]
        ]
        tag_links = f'<div class="tags" style="margin:0 0 26px">{"".join(links)}</div>'

    heading = f"Posts tagged “{e(tag)}”" if tag else "Blog"
    body = f"""
<main><div class="wrap">
  <div class="kicker">StocksDeepDive</div>
  <h1>{heading}</h1>
  <p class="lede">Research notes and valuation walk-throughs - the reasoning
  behind the numbers the site computes, written out in full.</p>
  {tag_links}
  {''.join(cards)}
  <div class="cta">
    <h3>Run the numbers yourself</h3>
    <p>Every figure discussed here comes from the same engine you can point at
    any ticker &mdash; <a href="/deep-dive">Stock Deep Dive</a>,
    <a href="/comparison">side-by-side Comparison</a> or the
    <a href="/scanner">Stock Scanner</a>.</p>
  </div>
</div></main>
"""
    head = _head(title, desc, canonical, base_url, noindex=noindex,
                 json_ld=_index_json_ld(posts, base_url))
    return _page(head, body)


def render_post(post, base_url, prev_post=None, next_post=None):
    e = html.escape
    url = post_url(base_url, post["slug"])
    desc = post_description(post)
    is_draft = post.get("status") != blog_store.STATUS_PUBLISHED

    meta_bits = []
    if post.get("published_at"):
        meta_bits.append(f"Published {_human_date(post['published_at'])}")
    if (post.get("updated_at") and post.get("published_at")
            and post["updated_at"][:10] != post["published_at"][:10]):
        meta_bits.append(f"updated {_human_date(post['updated_at'])}")
    meta_bits.append(f"{reading_time(post.get('body_md'))} min read")
    if post.get("author"):
        meta_bits.append(f"by {post['author']}")

    hero = ""
    h = hero_url(base_url, post)
    if h:
        hero = (f'<figure><img src="{e(h)}" alt="'
                f'{e(post.get("hero_alt") or post["title"])}" '
                f'width="1200" height="630" loading="eager"></figure>')

    tags = ""
    tl = blog_store.tag_list(post)
    if tl:
        tags = ('<div class="tags">' + "".join(
            f'<a class="tag" href="/blog?tag={e(t)}">{e(t)}</a>' for t in tl
        ) + "</div>")

    nav = []
    if prev_post:
        nav.append(f'<a href="/blog/{e(prev_post["slug"])}">&larr; '
                   f'{e(prev_post["title"])}</a>')
    if next_post:
        nav.append(f'<a href="/blog/{e(next_post["slug"])}">'
                   f'{e(next_post["title"])} &rarr;</a>')
    nav_html = (f'<hr><div class="meta" style="display:flex;gap:26px;'
                f'flex-wrap:wrap">{"".join(nav)}</div>' if nav else "")

    draft_banner = ""
    if is_draft:
        draft_banner = ('<div class="cta" style="border-left-color:#fb7185;'
                        'margin:0 0 26px"><h3>Draft preview</h3><p>This post is '
                        'not published. It is hidden from the blog index, the '
                        'sitemap and the feed, and is marked noindex.</p></div>')

    body = f"""
<main><div class="wrap">
  <article>
    {draft_banner}
    <div class="kicker"><a href="/blog" style="color:#2dd4bf">Blog</a></div>
    <h1>{e(post['title'])}</h1>
    <div class="meta">{e(' · '.join(meta_bits))}</div>
    {hero}
    {md_to_html(post.get('body_md'))}
    {tags}
  </article>
  <div class="cta">
    <h3>Check any of this against the live numbers</h3>
    <p>Put a ticker into <a href="/deep-dive">Stock Deep Dive</a> and the same
    valuation, quality and psychology maths described here runs on it &mdash;
    with every input shown next to the score. See
    <a href="/methodology">how the scores work</a>.</p>
  </div>
  {nav_html}
</div></main>
"""
    head = _head(
        title=f"{post['title']} | {SITE_NAME}",
        description=desc,
        canonical=url,
        base_url=base_url,
        image=hero_url(base_url, post),
        noindex=is_draft,
        extra_meta=(
            f'<meta property="og:type" content="article">\n'
            f'<meta property="article:published_time" '
            f'content="{html.escape(_iso_date(post.get("published_at")))}">\n'
            f'<meta property="article:modified_time" '
            f'content="{html.escape(_iso_date(post.get("updated_at")))}">'
        ),
        json_ld=_post_json_ld(post, base_url),
    )
    return _page(head, body)


def render_not_found(base_url, message="That page doesn't exist."):
    body = f"""
<main><div class="wrap">
  <div class="kicker">404</div>
  <h1>Not found</h1>
  <p class="lede">{html.escape(message)}</p>
  <p><a href="/blog">Back to the blog</a> &nbsp;&middot;&nbsp;
     <a href="/">Go to StocksDeepDive</a></p>
</div></main>
"""
    head = _head("Not found | " + SITE_NAME, "This page does not exist.",
                 f"{base_url}/blog", base_url, noindex=True)
    return _page(head, body)


# -----------------------------------
# MACHINE-READABLE ENDPOINTS
# -----------------------------------

def render_sitemap(posts, base_url):
    """XML sitemap covering the app's pages and every published post. This
    is what gets submitted in Google Search Console; without it a Streamlit
    site has essentially no discoverable URL surface at all."""
    rows = []
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    for path, priority, freq in APP_PATHS:
        rows.append(
            f"  <url><loc>{xml_escape(base_url + path)}</loc>"
            f"<changefreq>{freq}</changefreq>"
            f"<priority>{priority}</priority></url>"
        )
    blog_mod = (blog_store.last_modified() or "")[:10] or today
    rows.append(
        f"  <url><loc>{xml_escape(base_url)}/blog</loc>"
        f"<lastmod>{blog_mod}</lastmod>"
        f"<changefreq>weekly</changefreq><priority>0.9</priority></url>"
    )
    for p in posts:
        lastmod = (p.get("updated_at") or p.get("published_at") or "")[:10]
        rows.append(
            f"  <url><loc>{xml_escape(post_url(base_url, p['slug']))}</loc>"
            + (f"<lastmod>{lastmod}</lastmod>" if lastmod else "")
            + "<changefreq>monthly</changefreq><priority>0.8</priority></url>"
        )
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
            + "\n".join(rows) + "\n</urlset>\n")


def render_robots(base_url):
    return (
        "User-agent: *\n"
        "Allow: /\n"
        # The admin editor and Streamlit's internal endpoints are noise in
        # an index and should never be crawled.
        "Disallow: /blog-admin\n"
        "Disallow: /_stcore/\n"
        "Disallow: /*?admin=\n"
        "\n"
        f"Sitemap: {base_url}/sitemap.xml\n"
    )


def render_feed(posts, base_url):
    """RSS 2.0 - cheap to produce, and the thing readers and aggregators
    (and a few crawlers) look for once a blog exists."""
    items = []
    for p in posts[:30]:
        url = post_url(base_url, p["slug"])
        pub = ""
        if p.get("published_at"):
            try:
                dt = datetime.fromisoformat(
                    p["published_at"].replace("Z", "+00:00"))
                pub = dt.strftime("%a, %d %b %Y %H:%M:%S +0000")
            except Exception:
                pass
        items.append(
            "    <item>\n"
            f"      <title>{xml_escape(p['title'])}</title>\n"
            f"      <link>{xml_escape(url)}</link>\n"
            f"      <guid isPermaLink=\"true\">{xml_escape(url)}</guid>\n"
            f"      <description>{xml_escape(post_description(p))}</description>\n"
            + (f"      <pubDate>{pub}</pubDate>\n" if pub else "")
            + "    </item>"
        )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">\n'
        "  <channel>\n"
        f"    <title>{xml_escape(SITE_NAME)} Blog</title>\n"
        f"    <link>{xml_escape(base_url)}/blog</link>\n"
        f"    <description>{xml_escape(INDEX_DESC)}</description>\n"
        "    <language>en</language>\n"
        f'    <atom:link href="{xml_escape(base_url)}/blog/feed.xml" '
        'rel="self" type="application/rss+xml"/>\n'
        + "\n".join(items) + "\n"
        "  </channel>\n</rss>\n"
    )
