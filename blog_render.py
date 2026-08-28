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
from email.utils import format_datetime as _rfc2822
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
    URL.

    canonical=None (audit fix 4.6) omits BOTH the <link rel="canonical">
    tag and og:url - used by render_not_found(), which used to pass
    "{base_url}/blog" as an unrelated page's canonical for every 404,
    asserting the wrong page is the "real" URL for whatever was actually
    requested. Mostly inert (crawlers largely ignore canonicals on error
    pages) but not a meaningful value as written, so dropped rather than
    threading the originally-requested path through just to
    self-reference it."""
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
    ]
    if canonical:
        tags.append(f'<link rel="canonical" href="{e(canonical)}">')
    tags += [
        ('<meta name="robots" content="noindex,follow">' if noindex else
         '<meta name="robots" content="index,follow,max-image-preview:large,'
         'max-snippet:-1">'),
        f'<meta property="og:site_name" content="{e(SITE_NAME)}">',
        f'<meta property="og:title" content="{e(title)}">',
        f'<meta property="og:description" content="{e(description)}">',
    ]
    if canonical:
        tags.append(f'<meta property="og:url" content="{e(canonical)}">')
    tags += [
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


_HOME_CSS = """
.home main{padding:26px 0 0}
.home .wrap{max-width:1080px}
.hero{display:flex;gap:56px;flex-wrap:wrap;align-items:flex-start;margin:6px 0 44px}
.hero-l{flex:1 1 460px}
.hero-r{flex:1 1 340px}
.h1{font-size:43px;line-height:1.13;font-weight:800;letter-spacing:-.7px;margin:0 0 16px}
.h1 em{font-style:normal;color:#2dd4bf}
.sub{font-size:17.5px;color:#b9c9dc;margin:0 0 24px;max-width:34em}
.sub b{color:#e6edf5}
form.search{display:flex;gap:10px;flex-wrap:wrap;margin:0 0 12px}
form.search input{flex:1 1 260px;background:#121f36;border:1px solid #1f3352;border-radius:9px;
  padding:12px 14px;color:#e6edf5;font-size:15px;font-family:inherit}
form.search input::placeholder{color:#5b7290}
form.search input:focus{outline:none;border-color:#2dd4bf}
form.search button{background:#2dd4bf;color:#06231f;border:0;border-radius:9px;
  padding:12px 26px;font-size:15px;font-weight:700;cursor:pointer;font-family:inherit}
form.search button:hover{filter:brightness(1.08)}
.chips{display:flex;gap:8px;flex-wrap:wrap;margin:0 0 8px}
.chip{background:#121f36;border:1px solid #1f3352;border-radius:999px;padding:5px 14px;
  font-size:13px;color:#b9c9dc}
.chip:hover{border-color:#2dd4bf;text-decoration:none;color:#e6edf5}
.fineprint{color:#5b7290;font-size:12.5px;margin:0}
.h2{font-size:29px;font-weight:800;letter-spacing:-.3px;margin:0 0 8px}
.secsub{color:#8aa0b8;font-size:15.5px;margin:0 0 22px;max-width:46em}
.grid4{display:grid;grid-template-columns:repeat(auto-fit,minmax(232px,1fr));gap:16px}
.feat{display:block;background:#121f36;border:1px solid #1f3352;border-radius:12px;
  padding:20px;color:inherit;text-decoration:none;transition:border-color .15s,transform .15s}
.feat:hover{border-color:#2dd4bf;text-decoration:none;transform:translateY(-2px)}
.feat .ic{font-size:22px;margin-bottom:8px}
.feat h3{font-size:16.5px;margin:0 0 7px;color:#e6edf5}
.feat p{font-size:14px;color:#8aa0b8;margin:0;line-height:1.6}
.steps{display:grid;grid-template-columns:repeat(auto-fit,minmax(250px,1fr));gap:22px;margin:0 0 22px}
.step .n{font-family:ui-monospace,Menlo,monospace;color:#2dd4bf;font-size:12px}
.step h4{margin:6px 0 6px;font-size:16.5px;color:#e6edf5}
.step p{font-size:14.5px;color:#8aa0b8;margin:0;line-height:1.62}
.honesty{background:#121f36;border:1px solid #1f3352;border-left:3px solid #fb7185;
  border-radius:10px;padding:16px 20px;color:#b9c9dc;font-size:14.5px;line-height:1.6}
.honesty b{color:#e6edf5}
.covgrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:14px}
.cov{background:#121f36;border:1px solid #1f3352;border-radius:12px;padding:16px 18px;
  color:inherit;text-decoration:none;display:block}
.cov:hover{border-color:#2dd4bf;text-decoration:none}
.cov .tkr{font-family:ui-monospace,Menlo,monospace;font-weight:700;font-size:15px;color:#e6edf5}
.cov .ind{color:#5b7290;font-size:12.5px;margin:2px 0 10px}
.cov .row{display:flex;justify-content:space-between;font-size:13px;color:#8aa0b8;margin-top:4px}
.cov .row b{color:#e6edf5;font-family:ui-monospace,Menlo,monospace}
section{margin:0 0 46px}
.kicker2{font-family:ui-monospace,Menlo,monospace;font-size:11.5px;letter-spacing:1.6px;
  text-transform:uppercase;color:#2dd4bf;margin:0 0 10px}
.postrow{display:grid;grid-template-columns:repeat(auto-fit,minmax(250px,1fr));gap:16px}
@media(max-width:640px){.h1{font-size:32px}.h2{font-size:24px}.hero{gap:28px}}
"""


def render_home(base_url, posts=None, coverage=None):
    """The homepage as real HTML.

    This is the page a search engine weighs most heavily and the one
    Streamlit hides most completely - the hero, the toolkit and the method
    are all streamed over a websocket, so a crawler sees an empty shell.
    Rendered here it is the same copy, in the first response, in about
    20KB.

    The live parts of the Streamlit home (ticker tape, featured analysis,
    market-mood strip, signed-in watchlist) are deliberately not
    reproduced: they can't exist without a session, and a crawler would
    never see them anyway. Anyone who wants them - or the account bar -
    gets there through "Open the live app", which loads the Streamlit home
    exactly as before. server.py only serves this page for a bare "/"; any
    query string at all (?src=, ?admin=, the OAuth ?code= callback) is
    passed straight through to the app.
    """
    e = html.escape
    posts = posts or []

    cov_html = ""
    if coverage:
        cards = []
        for tkr in sorted(coverage):
            industry = coverage[tkr].get("industry") or ""
            sections = coverage[tkr].get("sections") or 1
            cards.append(
                f'<a class="cov" href="/research?ticker={e(tkr)}">'
                f'<div class="tkr">{e(tkr)}</div>'
                f'<div class="ind">{e(str(industry))}</div>'
                f'<div class="row"><span>Research sections</span><b>{sections}</b></div>'
                f'<div class="row"><span>Written verdict</span>'
                f'<b style="color:#34d399">&#10003;</b></div></a>'
            )
        cards.append(
            '<a class="cov" href="/research">'
            '<div style="font-size:22px;color:#2dd4bf">&#65291;</div>'
            'Which stock should be researched next?<br>'
            '<span style="color:#2dd4bf;font-weight:600">Tell us via Feedback &rarr;</span></a>'
        )
        cov_html = f"""
  <section>
    <div class="kicker2">Rational Compounder Research</div>
    <div class="h2">Covered in depth today</div>
    <div class="secsub">New companies are added as the research completes &mdash; each one
    takes weeks, not minutes.</div>
    <div class="covgrid">{''.join(cards)}</div>
  </section>"""

    posts_html = ""
    if posts:
        cards = "".join(
            f'<a class="card" href="/blog/{e(p["slug"])}"><h2>{e(p["title"])}</h2>'
            f'<p>{e(post_description(p))}</p>'
            f'<div class="meta">{e(_human_date(p.get("published_at")))} · '
            f'{reading_time(p.get("body_md"))} min read</div></a>'
            for p in posts[:3]
        )
        posts_html = f"""
  <section>
    <div class="kicker2">From the blog</div>
    <div class="h2">Written this month</div>
    <div class="secsub">The reasoning behind the numbers, written out in full &mdash;
    <a href="/blog">all posts</a>.</div>
    <div class="postrow">{cards}</div>
  </section>"""

    body = f"""
<main><div class="wrap">
  <div class="hero">
    <div class="hero-l">
      <h1 class="h1">The <em>data and models</em> behind a valuation judgment.</h1>
      <p class="sub">Live intrinsic values, quality calculations, psychology and discovery
      readings &mdash; computed for any ASX or US stock, with <b>every input stated and every
      estimate flagged</b>. The judgment stays yours.</p>
      <form class="search" action="/deep-dive" method="get" id="tickerform">
        <input type="text" name="ticker" id="tickerinput" autocomplete="off"
               placeholder="CSL.AX  ·  or two tickers to compare: CSL.AX BHP.AX"
               aria-label="Stock ticker">
        <button type="submit">Analyze</button>
      </form>
      <div class="chips">
        <a class="chip" href="/deep-dive?ticker=CSL.AX">CSL.AX</a>
        <a class="chip" href="/deep-dive?ticker=AAPL">AAPL</a>
        <a class="chip" href="/deep-dive?ticker=BHP.AX">BHP.AX</a>
        <a class="chip" href="/deep-dive?ticker=RMD.AX">RMD.AX</a>
        <a class="chip" href="/comparison?tickers=CSL.AX,BHP.AX">CSL.AX vs BHP.AX</a>
      </div>
      <p class="fineprint">One ticker = full Deep Dive &middot; Two or more = side-by-side
      Comparison &middot; ASX + US mixed freely</p>
    </div>
    <div class="hero-r">
      <div class="card" style="cursor:default">
        <div class="kicker">What you get</div>
        <p><b style="color:#e6edf5">What is the intrinsic value?</b> A live DCF with a
        per-stock discount rate, shown next to today's price with the margin of safety
        stated as a percentage.</p>
        <p><b style="color:#e6edf5">Is it a good business?</b> A 0&ndash;100 Quality Score
        from profitability and balance-sheet tests.</p>
        <p style="margin:0"><b style="color:#e6edf5">What is the crowd doing?</b> Psychology
        and discovery readings &mdash; distance from recent highs, volume, search and news
        attention &mdash; stated as numbers.</p>
      </div>
      <p class="fineprint" style="margin-top:12px">
        <a href="/?app=1">Open the live app &rarr;</a> for the ticker tape, today's featured
        analysis, market mood and your saved watchlist.</p>
    </div>
  </div>

  <section>
    <div class="kicker2">The toolkit</div>
    <div class="h2">Four ways in. One consistent model.</div>
    <div class="secsub">Every tool runs the same engine &mdash; the same DCF model, the same
    quality calculation, the same psychology read &mdash; so the numbers always agree with
    each other.</div>
    <div class="grid4">
      <a class="feat" href="/deep-dive">
        <div class="ic">&#128269;</div><h3>Stock Deep Dive</h3>
        <p>The full picture for one ticker: intrinsic value vs today's price, what drives the
        Value Score, and psychology and discovery readings &mdash; every input stated.</p></a>
      <a class="feat" href="/comparison">
        <div class="ic">&#9878;&#65039;</div><h3>Side-by-side Comparison</h3>
        <p>Two or more tickers lined up on identical calculations &mdash; intrinsic value,
        quality calculation, psychology &mdash; as colour-coded data bars.</p></a>
      <a class="feat" href="/scanner">
        <div class="ic">&#128225;</div><h3>Stock Scanner</h3>
        <p>A whole index &mdash; ASX 200, S&amp;P 500 and more &mdash; as one sortable data
        table, computed nightly, with an optional sector filter.</p></a>
      <a class="feat" href="/research">
        <div class="ic">&#128218;</div><h3>Rational Compounder Research</h3>
        <p>Hand-built research on selected compounders &mdash; a decade of reported earnings,
        four fair-value models, and documented company histories.</p></a>
    </div>
  </section>

  <section>
    <div class="kicker2">How it works</div>
    <div class="h2">Search. Compute. Inspect.</div>
    <div class="steps">
      <div class="step"><div class="n">01</div><h4>Type any ticker</h4>
        <p>ASX (CSL.AX) or US (AAPL). Live data is pulled on the spot &mdash; prices, cash
        flows, news, search trends, social chatter.</p></div>
      <div class="step"><div class="n">02</div><h4>Get one transparent calculation</h4>
        <p>The Value Score blends the quality calculation, the gap between price and intrinsic
        value, psychology and discovery &mdash; the same arithmetic every time, with every
        input shown.</p></div>
      <div class="step"><div class="n">03</div><h4>See value AND psychology</h4>
        <p>Two separate calculations, never blurred: what the model computes from the
        business's own cash flows, and what the crowd has been doing to the price.</p></div>
    </div>
    <div class="honesty"><b>The red-flag rule:</b> whenever a number rests on a default or
    average because real data wasn't available, it's shown in red. An estimate is never
    dressed up as a fact &mdash; you always know which numbers are computed and which are
    assumed. <a href="/methodology">How the scores work &rarr;</a></div>
  </section>
{cov_html}{posts_html}
  <section>
    <div class="cta">
      <h3>Everything is free.</h3>
      <p><a href="/?app=1">Open the app</a> and sign in to save a watchlist and get the
      weekly watchlist digest.</p>
    </div>
  </section>
</div></main>
<script>
// Two or more tickers belong in Comparison, one in Deep Dive. Without
// JavaScript the form still works - it just always lands on Deep Dive.
document.getElementById('tickerform').addEventListener('submit', function (ev) {{
  var raw = document.getElementById('tickerinput').value.trim();
  var parts = raw.split(/[\\s,]+/).filter(Boolean);
  if (parts.length > 1) {{
    ev.preventDefault();
    window.location = '/comparison?tickers=' + encodeURIComponent(parts.join(','));
  }}
}});
</script>
"""
    description = ("Live intrinsic value, quality, crowd psychology and discovery "
                   "readings for any ASX or US stock - every input stated and every "
                   "estimate flagged. Free.")
    json_ld = _json_ld([
        {"@context": "https://schema.org", "@type": "WebSite",
         "name": SITE_NAME, "url": base_url,
         "description": description,
         "potentialAction": {
             "@type": "SearchAction",
             "target": {"@type": "EntryPoint",
                        "urlTemplate": f"{base_url}/deep-dive?ticker={{search_term_string}}"},
             "query-input": "required name=search_term_string"}},
        {"@context": "https://schema.org", "@type": "Organization",
         "name": SITE_NAME, "url": base_url,
         "email": "rationalcompounder@stocksdeepdive.com",
         "founder": {"@type": "Person", "name": "Andres Moreno"}},
    ])
    head = _head(
        f"{SITE_NAME} - what a stock is worth, with every input shown",
        description, base_url + "/", base_url, json_ld=json_ld,
        extra_meta=f"<style>{_HOME_CSS}</style>",
    )
    return _page(head, body).replace("<body>", '<body class="home">', 1)


# -----------------------------------
# TOOL LANDING PAGES
#
# /deep-dive, /comparison, /scanner and /research are live Streamlit
# tools, so a crawler fetching them gets an empty shell. But the bare URL
# - the one with no ticker on it - isn't a result page at all; it is the
# tool's front door, and it is exactly the page that should rank for
# "ASX stock valuation tool" or "compare two stocks".
#
# So the bare URL is served from here as a real page explaining what the
# tool does, with a form that hands straight off into the live app. The
# moment a ticker is on the URL (?ticker=, ?tickers=, ?app=1) the request
# is proxied to Streamlit exactly as before - see server.py. Nothing is
# hidden from users that isn't shown to crawlers: everyone gets this page
# for a bare URL, and the same app for a real query.
# -----------------------------------

TOOL_PAGES = {
    "/deep-dive": {
        "title": "Stock Deep Dive - intrinsic value for any ASX or US stock",
        "h1": "Stock Deep Dive",
        "description": ("Put in one ticker and get a live discounted cash flow, a "
                        "quality score from reported fundamentals, and crowd "
                        "psychology readings - with every input shown."),
        "lede": ("One ticker in, the whole picture out: what a discounted cash "
                 "flow says the business is worth, how good a business it "
                 "actually is, and what the crowd has been doing to the price."),
        "form": {"action": "/deep-dive", "field": "ticker",
                 "placeholder": "CSL.AX  ·  AAPL  ·  BHP.AX",
                 "button": "Analyze"},
        "sections": [
            ("What it computes", [
                "**Intrinsic value.** A discounted cash flow built from the "
                "company's own reported free cash flows, with the discount rate "
                "calculated per stock from its own beta against its market "
                "(CAPM), growth taken from analyst consensus where available and "
                "the company's own FCF history otherwise, and a terminal growth "
                "rate set by the stock's currency. Where a DCF isn't possible a "
                "P/E-blend fallback is used, and labelled as such.",
                "**Quality.** Return on equity, profit margin, revenue and "
                "earnings growth, free cash flow and debt, computed from reported "
                "fundamentals. Loss-making, cash-burning businesses are capped - "
                "a company that doesn't make money can't score as high quality no "
                "matter how fast it grows.",
                "**Psychology and discovery.** Distance below the three-month "
                "high, distance from the 50-day average, volume, search interest, "
                "news flow and social chatter - reported as measurements, "
                "deliberately kept separate from the valuation.",
            ]),
            ("Why every estimate is flagged", [
                "Whenever a number rests on a default or an average because real "
                "data wasn't available, it is shown in red. You always know which "
                "figures are computed from the company's own filings and which "
                "are assumptions - so you can disagree with the assumption "
                "instead of inheriting it. Several of them you can override "
                "yourself and watch the valuation move.",
            ]),
            ("Coverage", [
                "Any ASX ticker (`CSL.AX`, `BHP.AX`) and any US ticker (`AAPL`, "
                "`MSFT`). Data is pulled live at the moment you search, from "
                "Yahoo Finance, Google Trends, NewsAPI and StockTwits.",
            ]),
        ],
    },
    "/comparison": {
        "title": "Compare stocks side by side - identical valuation maths",
        "h1": "Side-by-side Comparison",
        "description": ("Line two or more ASX or US stocks up on identical "
                        "calculations - intrinsic value, quality, psychology - as "
                        "colour-coded bars, so the comparison is like for like."),
        "lede": ("Two or more tickers, the same arithmetic applied to each, laid "
                 "out in one table. The point is that nothing is computed "
                 "differently for one stock than for another."),
        "form": {"action": "/comparison", "field": "tickers",
                 "placeholder": "CSL.AX, BHP.AX  ·  AAPL, MSFT, GOOGL",
                 "button": "Compare"},
        "sections": [
            ("Why like-for-like matters", [
                "Most comparisons are assembled from whatever number each source "
                "happened to publish - one company's P/E from a broker note, "
                "another's from a screener with a different definition of "
                "earnings. Here every stock in the table goes through the same "
                "DCF, the same quality tests and the same psychology read, so a "
                "difference in the output is a difference in the business rather "
                "than a difference in the method.",
            ]),
            ("What you can line up", [
                "Intrinsic value against today's price, margin of safety as a "
                "percentage, the quality calculation and its components, and the "
                "psychology and discovery readings. Mix ASX and US tickers "
                "freely - currency and terminal growth are handled per stock.",
            ]),
            ("Reading the bars", [
                "Each metric is drawn as a colour-coded bar so the ranking is "
                "visible before you read a single number, and any value resting "
                "on an estimate is flagged in red rather than quietly averaged "
                "in.",
            ]),
        ],
    },
    "/scanner": {
        "title": "Stock Scanner - rank a whole index by the same model",
        "h1": "Stock Scanner",
        "description": ("Rank the ASX 200, S&P 500 and other indices on one "
                        "consistent valuation model, computed nightly, with an "
                        "optional sector filter."),
        "lede": ("A whole index as one sortable table - the same valuation, "
                 "quality and psychology maths run across every constituent, "
                 "recomputed overnight."),
        "form": None,
        "sections": [
            ("What it's for", [
                "A deep dive answers a question about a company you already have "
                "in mind. The scanner is the other direction: it is for finding "
                "the companies worth having in mind in the first place. Sort a "
                "whole index by the same score, filter to a sector, and start "
                "from the top.",
            ]),
            ("How the numbers get there", [
                "The scan runs overnight across each configured universe, so "
                "opening the page shows a finished table rather than starting "
                "hundreds of live data pulls. Every column is the same "
                "calculation described in [how the scores work]"
                "(/methodology) - sorting the table is arithmetic, not opinion.",
            ]),
            ("Universes", [
                "ASX 200, S&P 500 and other indices, with an optional sector "
                "filter on top. Any row can be opened straight into a full "
                "[Deep Dive](/deep-dive) on that ticker.",
            ]),
        ],
    },
    "/research": {
        "title": "Rational Compounder Research - hand-built company analysis",
        "h1": "Rational Compounder Research",
        "description": ("Hand-built, Buffett/Munger-style research on selected "
                        "quality compounders: a decade of reported earnings, four "
                        "fair-value methods and written judgment on each business."),
        "lede": ("The one part of this site that isn't computed. Each company "
                 "here is a workbook the author built by hand over weeks - a "
                 "decade of reported earnings, four independent fair-value "
                 "methods, and written judgment on management, moat and risk."),
        "form": None,
        "sections": [
            ("How it differs from the tools", [
                "Everywhere else on this site, a ticker goes in and the same "
                "engine runs. The research section is the opposite: every "
                "threshold, every colour band and every verdict comes from the "
                "original workbook for that specific company, not from a generic "
                "screen. It is slow on purpose - each company takes weeks.",
            ]),
            ("What each company gets", [
                "Ten years of reported earnings and margins; four independent "
                "fair-value estimates (trailing P/E, forward P/E, a discounted "
                "cash flow, and a ten-year equity method) shown side by side "
                "rather than averaged into one false number; and written "
                "Buffett/Munger-style judgment on the business, its management "
                "and what would break the thesis.",
            ]),
            ("Author position disclosure", [
                "Each covered company states whether the author personally "
                "holds it, has never held it, or previously held and exited - "
                "next to the research, not buried. Skin in the game is context "
                "you're entitled to when reading someone's opinion.",
            ]),
        ],
    },
}


def render_tool_landing(path, base_url, coverage=None):
    e = html.escape
    spec = TOOL_PAGES[path]

    form_html = ""
    if spec["form"]:
        f = spec["form"]
        form_html = f"""
  <form class="search" action="{f['action']}" method="get">
    <input type="text" name="{f['field']}" autocomplete="off"
           placeholder="{e(f['placeholder'])}" aria-label="Stock ticker">
    <button type="submit">{e(f['button'])}</button>
  </form>"""
    else:
        form_html = (f'<p><a class="chip" href="{path}?app=1">'
                     f'Open {e(spec["h1"])} &rarr;</a></p>')

    sections = "".join(
        f'<h2>{e(h)}</h2>' + md_to_html("\n\n".join(paras))
        for h, paras in spec["sections"]
    )

    cov_html = ""
    if coverage and path == "/research":
        items = "".join(
            f'<a class="cov" href="/research?ticker={e(t)}">'
            f'<div class="tkr">{e(t)}</div>'
            f'<div class="ind">{e(str(coverage[t].get("industry") or ""))}</div>'
            f'<div class="row"><span>Research sections</span>'
            f'<b>{coverage[t].get("sections", 1)}</b></div></a>'
            for t in sorted(coverage)
        )
        cov_html = (f'<h2>Covered in depth today</h2>'
                    f'<div class="covgrid">{items}</div>')

    body = f"""
<main><div class="wrap">
  <article>
    <h1>{e(spec['h1'])}</h1>
    <p class="lede">{e(spec['lede'])}</p>
    {form_html}
    {sections}
    {cov_html}
  </article>
  <div class="cta">
    <h3>The same engine runs everywhere</h3>
    <p>Read <a href="/methodology">how the scores work</a>, or try
    <a href="/deep-dive">Deep Dive</a>,
    <a href="/comparison">Comparison</a>,
    <a href="/scanner">Scanner</a> and
    <a href="/research">Rational Compounder Research</a>. Longer write-ups
    live on the <a href="/blog">blog</a>.</p>
  </div>
</div></main>
"""
    canonical = f"{base_url}{path}"
    json_ld = _json_ld({
        "@context": "https://schema.org",
        "@type": "WebApplication",
        "name": spec["h1"],
        "url": canonical,
        "description": spec["description"],
        "applicationCategory": "FinanceApplication",
        "operatingSystem": "Any (web)",
        "offers": {"@type": "Offer", "price": "0", "priceCurrency": "AUD"},
        "publisher": {"@type": "Organization", "name": SITE_NAME, "url": base_url},
    })
    head = _head(f"{spec['title']} | {SITE_NAME}", spec["description"],
                 canonical, base_url, json_ld=json_ld,
                 extra_meta=f"<style>{_HOME_CSS}</style>")
    return _page(head, body).replace("<body>", '<body class="home">', 1)


def render_content_page(title, markdown_text, description, path, base_url,
                        heading=None, intro_note=None):
    """A standing content page (How the scores work / About / Privacy) as
    real HTML.

    These pages exist in the Streamlit app too, but a crawler fetching
    them there gets an empty shell. Served from here they are the site's
    only substantive indexable pages besides the blog - and 'how the
    scores work' is the page that explains the whole product, so it is the
    one most worth ranking. The prose comes from site_content.py, the same
    source the app renders, so the two can never drift."""
    note = ""
    if intro_note:
        note = (f'<div class="cta" style="margin:0 0 26px">'
                f'{md_to_html(intro_note)}</div>')
    body = f"""
<main><div class="wrap">
  <article>
    <h1>{html.escape(heading or title)}</h1>
    {note}
    {md_to_html(markdown_text)}
  </article>
  <div class="cta">
    <h3>See it run on a real company</h3>
    <p>Put a ticker into <a href="/deep-dive">Stock Deep Dive</a>, line two up
    <a href="/comparison">side by side</a>, or read the hand-built
    <a href="/research">Rational Compounder research</a>. New writing lands on
    the <a href="/blog">blog</a>.</p>
  </div>
</div></main>
"""
    canonical = f"{base_url}{path}"
    json_ld = _json_ld({
        "@context": "https://schema.org",
        "@type": "WebPage",
        "name": title,
        "description": description,
        "url": canonical,
        "isPartOf": {"@type": "WebSite", "name": SITE_NAME, "url": base_url},
        "publisher": {"@type": "Organization", "name": SITE_NAME,
                      "url": base_url},
        "inLanguage": "en",
    })
    head = _head(f"{title} | {SITE_NAME}", description, canonical, base_url,
                 json_ld=json_ld)
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
                 None, base_url, noindex=True)
    return _page(head, body)


# -----------------------------------
# MACHINE-READABLE ENDPOINTS
# -----------------------------------

def render_sitemap(posts, base_url, renders_html=None):
    """XML sitemap covering the app's pages and every published post. This
    is what gets submitted in Google Search Console; without it a Streamlit
    site has essentially no discoverable URL surface at all.

    renders_html: optional callable(path) -> bool - the server's own test
    for whether a path is served as real HTML. When provided, app pages
    that would only serve the empty Streamlit shell are LEFT OUT of the
    sitemap: a sitemap must never advertise a URL a crawler will find
    blank. This now includes "/" itself (audit fix 4.2) - it used to be
    special-cased as "always listed" on the assumption the domain root
    gets crawled regardless of the sitemap, but the live "/" route only
    serves real HTML when INDEXABLE_PAGES includes it, and the module's
    own documented example config (INDEXABLE_PAGES=/methodology,/about,
    /privacy) omits it - under that exact config this used to assert
    priority=1.0, changefreq=daily for a URL that actually proxies to an
    empty JS shell. "/" now goes through the identical renders_html()
    check as every other app path."""
    rows = []
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    for path, priority, freq in APP_PATHS:
        if renders_html is not None and not renders_html(path):
            continue
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
                # Audit fix 4.5: %a/%b render in the PROCESS locale, but
                # RFC 2822 (what RSS pubDate requires) mandates English
                # day/month abbreviations regardless of locale - a non-
                # English locale here would emit a spec-invalid date that
                # readers/aggregators may reject or mis-parse.
                # email.utils.format_datetime() is stdlib and always
                # locale-independent.
                pub = _rfc2822(dt)
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
