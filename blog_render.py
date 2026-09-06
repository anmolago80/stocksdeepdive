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
import json
import os
import re
from datetime import datetime, timezone
from email.utils import format_datetime as _rfc2822
from xml.sax.saxutils import escape as xml_escape

import blog_store
import follow_store
import snapshot_store

SITE_NAME = "StocksDeepDive"

# AI-readiness roadmap Phase 4 (citation helpers): the one place the
# author's name is spelled for structured data, reused by every JSON-LD
# block below via _person_json_ld()/_organization_json_ld() rather than
# each page hand-rolling its own partial Organization dict (which is how
# this drifted before - the homepage's Organization block had an email
# and a founder Person; every other page's did not, and posts with no
# explicit author fell back to a Person literally named "StocksDeepDive").
# Matches the prose already on /about and /methodology (site_content.py)
# and server.py's own site-description string - not a new fact, just the
# first place it's centralised for schema use.
AUTHOR_NAME = "Andres Moreno"
DEFAULT_AUTHOR = AUTHOR_NAME

# Mega-batch Part 18: the small amber "NEW" dot beside the 🧰 Tools nav
# tab, for the launch period only - matches app.py's own
# NAV_TOOLS_NEW_BADGE_ENABLED constant; flip both together when Tools is
# no longer new.
NAV_TOOLS_NEW_BADGE_ENABLED = True

# Amendment to Part 5 / i18n (owner, 6 Sep): tiny inline SVG flags beside
# the EN|ES nav toggle - NOT emoji flags, since Windows browsers don't
# render country-flag emoji (a visitor there would just see "GB ES"
# letters instead of a flag). ~16x11px, simplified (a plain three-stripe
# Spanish flag; a simplified Union Jack - solid navy with a white+red
# cross, not the exact counter-changed diagonal construction of the real
# flag, which is illegible at this size anyway), inlined as literal SVG
# markup rather than an external file/request. Decorative only
# (aria-hidden) - the EN/ES text stays for accessibility and clarity, and
# nothing about how the language itself resolves changes.
_FLAG_GB_SVG = ('<svg class="flag" viewBox="0 0 32 22" aria-hidden="true" '
                'focusable="false"><rect width="32" height="22" fill="#012169"/>'
                '<path d="M0 0 L32 22 M32 0 L0 22" stroke="#FFFFFF" stroke-width="4.4"/>'
                '<path d="M0 0 L32 22 M32 0 L0 22" stroke="#C8102E" stroke-width="1.8"/>'
                '<path d="M16 0 V22 M0 11 H32" stroke="#FFFFFF" stroke-width="7.3"/>'
                '<path d="M16 0 V22 M0 11 H32" stroke="#C8102E" stroke-width="4.4"/></svg>')
_FLAG_ES_SVG = ('<svg class="flag" viewBox="0 0 32 22" aria-hidden="true" '
                'focusable="false"><rect width="32" height="22" fill="#AA151B"/>'
                '<rect y="5.5" width="32" height="11" fill="#F1BF00"/></svg>')

# Part 21 (owner-approved mock, small): the bordered bio card at the top
# of /about and /es/about - owner-supplied, hardcoded per the
# instruction's own words ("owner-supplied - hardcode this value as the
# constant"). Never change this without the owner asking.
ABOUT_LINKEDIN_URL = "https://www.linkedin.com/in/andreslarabridge/"

# Optional round photo beside the bio - a static/ file path constant; the
# shipped default is empty (no photo supplied yet), which means the bio
# card renders with NO photo/circle at all, never a broken image or a
# placeholder silhouette. Set to e.g. "/static/andres-moreno.jpg" (drop
# the file in this module's static/ dir) whenever the owner supplies one.
ABOUT_PHOTO_PATH = ""

# A small, decorative (aria-hidden) LinkedIn glyph - plain vector path
# data, not a fetched brand asset - beside the name line. The actual link
# text/destination lives on the surrounding <a>'s href and aria-label.
_LINKEDIN_ICON_SVG = '<svg viewBox="0 0 16 16" width="15" height="15" fill="currentColor" aria-hidden="true" focusable="false"><path d="M0 1.146C0 .513.526 0 1.175 0h13.65C15.474 0 16 .513 16 1.146v13.708c0 .633-.526 1.146-1.175 1.146H1.175C.526 16 0 15.487 0 14.854V1.146zm4.943 12.248V6.169H2.542v7.225h2.401zm-1.2-8.212c.837 0 1.358-.554 1.358-1.248-.015-.709-.52-1.248-1.342-1.248-.822 0-1.359.54-1.359 1.248 0 .694.521 1.248 1.327 1.248h.016zm4.908 8.212V9.359c0-.216.016-.432.08-.586.173-.431.568-.878 1.232-.878.869 0 1.216.662 1.216 1.634v3.865h2.401V9.25c0-2.22-1.184-3.252-2.764-3.252-1.274 0-1.845.7-2.165 1.193v.025h-.016l.016-.025V6.169h-2.4c.03.678 0 7.225 0 7.225h2.4z"/></svg>'

# Conversion pass, Part 5: the Reddit handle to display on the byline
# shown to visitors arriving with a Reddit-tagged src (see
# reddit_byline_visible/reddit_byline_html below). Confirmed with the
# owner per the instruction's own request to confirm this is the right
# handle to display.
REDDIT_HANDLE = "u/anmolago1"


def reddit_byline_visible(src):
    """True when `src` (st.session_state["first_src"] on the app side,
    the ?src= query param on a server-rendered blog page) is tagged for
    Reddit traffic - the same "reddit..." prefix convention the Part 6/7
    admin "Copy link for sharing" button writes (reddit-{ticker},
    reddit-rc, reddit-rc-{ticker}), so a share link built by that button
    is exactly what turns this on."""
    return bool((src or "").strip().lower().startswith("reddit"))


def reddit_byline_html(ticker=None):
    """One small "Built by the author of ... on Reddit" line, linking to
    /about - shared by the Deep Dive, the Research page and blog posts
    (conversion pass, Parts 5 and 7c) so the wording/handle can never
    drift between the three call sites. `ticker` is the ticker actually
    in view on the CALLING page (a Deep Dive's own ticker, a Research
    page's selected ticker, or a blog post's primary_ticker) - not
    reparsed out of the src string, since the sharing label's own
    convention (lowercase, no exchange suffix) can't always be reversed
    back to an exact exchange-qualified ticker, and the caller already
    knows its own ticker directly. Generic wording when there's no
    ticker in view (a bare "reddit"/"reddit-rc" src, or a post/page with
    no specific ticker)."""
    e = html.escape
    text = (f"Built by the author of the {e(ticker)} analysis on Reddit"
            if ticker else "Built by the author of this analysis on Reddit")
    return (
        '<div style="margin:6px 0 4px">'
        '<a href="/about" style="color:#8aa0b8;font-size:13px;'
        f'text-decoration:none">{text} &middot; {REDDIT_HANDLE}</a></div>'
    )


# -----------------------------------
# PWA (Part 1c): the same tag block is injected in two places -
# _head() below, for every page THIS module renders, and server.py's
# proxy response rewrite, for the proxied Streamlit shell - single source
# so the two paths can never drift apart. theme-color lives here (not as
# a separate literal in _head()'s tag list) for the same reason.
# manifest.webmanifest/icons/sw.js are all served by server.py directly
# (see its STATIC_DIR mount and the routes just above the blog routes).
#
# The trailing <script> also carries the Part 3a install nudge. It rides
# the SAME <head>-injected tag (rather than a second injection point
# aimed at </body>) because it only ever needs to run after `load`, by
# which point document.body definitely exists - one injection point, one
# script tag, same "never block page load" rule as the SW registration
# it sits next to.
# -----------------------------------
_INSTALL_NUDGE_JS = r"""
(function () {
  var KEY = 'sdd_install_nudge_count';
  function shownCount() { try { return parseInt(localStorage.getItem(KEY) || '0', 10); } catch (e) { return 99; } }
  function bumpShown() { try { localStorage.setItem(KEY, String(shownCount() + 1)); } catch (e) {} }
  function isStandalone() {
    return window.matchMedia('(display-mode: standalone)').matches || window.navigator.standalone === true;
  }
  function showBanner(text, actionLabel, onAction) {
    if (isStandalone() || shownCount() >= 2 || document.getElementById('sdd-install-nudge')) return;
    bumpShown();
    var bar = document.createElement('div');
    bar.id = 'sdd-install-nudge';
    bar.style.cssText = 'position:fixed;left:0;right:0;bottom:0;z-index:999999;'
      + 'background:#121f36;border-top:1px solid #2dd4bf;color:#e6edf5;'
      + 'padding:12px 14px;padding-bottom:calc(12px + env(safe-area-inset-bottom));'
      + 'font-family:"Segoe UI",system-ui,-apple-system,Roboto,Helvetica,Arial,sans-serif;'
      + 'font-size:13.5px;display:flex;align-items:center;gap:10px;'
      + 'box-shadow:0 -2px 10px rgba(0,0,0,.3);';
    var span = document.createElement('span');
    span.style.cssText = 'flex:1;line-height:1.4;';
    span.textContent = text;
    bar.appendChild(span);
    function closeBanner() { if (bar.parentNode) bar.parentNode.removeChild(bar); }
    if (actionLabel && onAction) {
      var btn = document.createElement('button');
      btn.textContent = actionLabel;
      btn.style.cssText = 'background:#2dd4bf;color:#0b1220;border:none;border-radius:8px;'
        + 'padding:9px 14px;font-weight:700;font-size:13px;cursor:pointer;min-height:40px;white-space:nowrap;';
      btn.onclick = function () { onAction(); closeBanner(); };
      bar.appendChild(btn);
    }
    var close = document.createElement('button');
    close.setAttribute('aria-label', 'Dismiss');
    close.textContent = '×';
    close.style.cssText = 'background:transparent;border:none;color:#8aa0b8;font-size:22px;'
      + 'line-height:1;cursor:pointer;padding:4px 6px;min-height:40px;min-width:40px;';
    close.onclick = closeBanner;
    bar.appendChild(close);
    document.body.appendChild(bar);
  }
  window.addEventListener('load', function () {
    if (isStandalone()) return;
    var isIOS = /iP(hone|ad|od)/.test(navigator.userAgent);
    if (isIOS && window.navigator.standalone === false) {
      showBanner("Add StocksDeepDive to your home screen: tap Share ⬆ then 'Add to Home Screen'.");
      return;
    }
    window.addEventListener('beforeinstallprompt', function (e) {
      e.preventDefault();
      showBanner('Install StocksDeepDive as an app on this device.', 'Install app', function () { e.prompt(); });
    });
  });
})();
"""

PWA_HEAD_TAGS = (
    '<meta name="theme-color" content="#0b1220">\n'
    '<link rel="manifest" href="/manifest.webmanifest">\n'
    '<link rel="icon" type="image/png" sizes="32x32" '
    'href="/pwa/icons/favicon-32.png">\n'
    '<link rel="apple-touch-icon" href="/pwa/icons/apple-touch-icon.png">\n'
    '<meta name="apple-mobile-web-app-capable" content="yes">\n'
    '<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">\n'
    '<meta name="apple-mobile-web-app-title" content="StocksDeepDive">\n'
    "<script>if('serviceWorker' in navigator){window.addEventListener('load',"
    "function(){navigator.serviceWorker.register('/sw.js').catch(function(){});});}"
    + _INSTALL_NUDGE_JS +
    "</script>"
)

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
    ("/how-we-use-ai", "0.4", "monthly"),
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


# -----------------------------------
# AI-readiness roadmap Phase 4 (citation helpers): a small "Copy citation"
# button, reused by blog posts, snapshot pages and the track-record page
# (track_record_render.py imports this same function). Plain client-side
# clipboard write - no library, no network request, nothing server-side to
# build or cache. The citation text is HTML-escaped into a data attribute
# rather than interpolated into the inline <script> itself, so nothing in
# a post title or ticker name can ever break out of the JS string.
# -----------------------------------

def _copy_citation_html(citation_text):
    e = html.escape
    return f"""
<div class="sdd-cite">
  <button type="button" class="sdd-cite-btn" id="sdd-cite-btn"
    data-citation="{e(citation_text)}">Copy citation</button>
</div>
<script>
(function(){{
  var b=document.getElementById('sdd-cite-btn');
  if(!b) return;
  b.addEventListener('click', function(){{
    var t=b.getAttribute('data-citation');
    var reset=b.textContent;
    function ok(){{b.textContent='Copied \\u2713';setTimeout(function(){{b.textContent=reset;}},1500);}}
    function fail(){{b.textContent='Copy failed';setTimeout(function(){{b.textContent=reset;}},1500);}}
    if(navigator.clipboard && navigator.clipboard.writeText){{
      navigator.clipboard.writeText(t).then(ok, fail);
    }} else {{ fail(); }}
  }});
}})();
</script>
"""


def _copy_as_text_html(copy_text, dom_id="sdd-copytext", label="Copy as text"):
    """Fix 4, AI fixes round 1 (2026-08-31): "Copy as text" button - same
    click-to-clipboard shape as _copy_citation_html() right above (a
    plain button + a small vanilla-JS IIFE, no framework), but this one
    additionally satisfies the spec's fallback requirement: where
    _copy_citation_html's failure path only changes the BUTTON's own
    label ("Copy failed"), clipboard access can be blocked entirely in
    some contexts (iOS inside Streamlit's components.html() sandboxed
    iframe is the specific case named in the spec) with nothing further
    the page can do about it - the visitor still needs the text, so on
    any failure this reveals a genuinely selectable, pre-filled,
    auto-selected <textarea> instead of just an error label.

    The textarea also IS the copy source (its own .value, read fresh on
    each click) rather than a separate data-* attribute - a <textarea>
    round-trips arbitrary multi-line text (quotes, newlines) through the
    DOM with zero manual JS-string escaping, which a single-line HTML
    attribute (as citation_text uses, being one line) can't do as
    cleanly for a multi-paragraph payload like this one. It starts
    positioned off-screen (not display:none - some browsers won't let a
    display:none element receive focus()/select(), which the fallback
    path needs) and is only moved on-screen if the clipboard call fails.

    dom_id lets a page render this more than once (e.g. a future page
    with two copy buttons) without id collisions; every caller today
    passes a ticker-qualified id already since the callers themselves
    are per-ticker (a Deep Dive page, a /s/<ticker> snapshot page).

    label (conversion pass, Part 6) overrides the button's own text -
    default "Copy as text" is unchanged for this function's two existing
    callers (app.py's _render_copy_as_text_button and blog_render's own
    citation-adjacent usage), so the admin "Copy link for sharing"
    button (Parts 6/7a) can pass label="Copy link" and reuse this exact
    click/clipboard/fallback-textarea mechanism instead of a second copy
    of it."""
    e = html.escape
    return f"""
<style>
/* Inlined here (duplicating the .sdd-cite/.sdd-cite-btn rules already in
   this file's own page-wide <style> block below) because this button
   isn't always rendered inside a page that has that stylesheet: app.py's
   Deep Dive page runs this whole snippet through
   streamlit.components.v1.html(), which puts it in its own isolated
   srcdoc iframe with NO inherited CSS from the parent Streamlit page or
   from anywhere else in this file - so without its own <style> tag the
   button fell back to the browser's unstyled default (a plain white/grey
   button), the exact "why does this look broken" spotted live on the
   Deep Dive page, 2026-09-01. On the static /s/<ticker> page (this same
   HTML dropped straight into snapshot_render.render_snapshot()'s body,
   which DOES already carry the page-wide stylesheet) this is simply a
   harmless duplicate of an identical rule - same selector, same values,
   last one wins, no visual difference. */
.sdd-cite{{margin:10px 0 22px}}
.sdd-cite-btn{{background:#121f36;border:1px solid #1f3352;border-radius:8px;
  color:#8aa0b8;font-size:12.5px;padding:6px 12px;cursor:pointer;font-family:inherit}}
.sdd-cite-btn:hover{{color:#e6edf5;border-color:#2dd4bf}}
</style>
<div class="sdd-cite">
  <button type="button" class="sdd-cite-btn" id="{e(dom_id)}-btn">{e(label)}</button>
  <div class="sdd-copytext-status" id="{e(dom_id)}-status"
    style="color:#8aa0b8;font-size:12.5px;margin-top:6px"></div>
  <textarea id="{e(dom_id)}-src" readonly
    style="position:absolute;left:-9999px;top:-9999px;width:1px;height:1px;"
  >{e(copy_text)}</textarea>
</div>
<script>
(function(){{
  var b = document.getElementById('{e(dom_id)}-btn');
  var ta = document.getElementById('{e(dom_id)}-src');
  var status = document.getElementById('{e(dom_id)}-status');
  if (!b || !ta) return;
  var reset = b.textContent;
  // Streamlit's components.v1.html() renders this whole snippet in its
  // own same-origin (srcdoc) iframe with a FIXED height Python declares
  // up front - it does not auto-size to content. That fixed height used
  // to be padded out to fit the rare "clipboard blocked, show the
  // fallback textarea" case (a much taller box), which left a large
  // empty gap below the button on every normal page load, spotted live
  // 2026-09-01 ("why is there a big vertical space here"). Because the
  // iframe is same-origin, this button's own script can reach out and
  // resize its own frame element directly - so the declared height only
  // needs to fit the common case, and this grows/shrinks the real frame
  // to match actual content on demand. On the static /s/<ticker> page
  // this same snippet is dropped straight into the page body (not an
  // iframe at all) - window.frameElement is simply null there, so this
  // is a safe no-op in that context.
  function resizeFrame(){{
    try {{
      if (window.frameElement) {{
        window.frameElement.style.height = (document.body.scrollHeight + 6) + 'px';
      }}
    }} catch (e) {{}}
  }}
  function hideTextarea(){{
    ta.style.position = 'absolute'; ta.style.left = '-9999px';
    ta.style.top = '-9999px'; ta.style.width = '1px'; ta.style.height = '1px';
    resizeFrame();
  }}
  function showTextarea(){{
    ta.style.position = 'static'; ta.style.width = '100%'; ta.style.height = '110px';
    ta.style.marginTop = '8px'; ta.style.background = '#0f1a2e';
    ta.style.color = '#e6edf5'; ta.style.border = '1px solid #1f3352';
    ta.style.borderRadius = '6px'; ta.style.padding = '8px';
    ta.style.fontFamily = 'ui-monospace,Menlo,monospace'; ta.style.fontSize = '11.5px';
    ta.focus();
    ta.select();
    resizeFrame();
  }}
  resizeFrame();
  b.addEventListener('click', function(){{
    var text = ta.value;
    if (navigator.clipboard && navigator.clipboard.writeText) {{
      navigator.clipboard.writeText(text).then(function(){{
        b.textContent = 'Copied \\u2713';
        status.textContent = '';
        hideTextarea();
        setTimeout(function(){{ b.textContent = reset; }}, 1500);
      }}, function(){{
        status.textContent = "Couldn't copy automatically - the text "
          + "below is selected, copy it manually.";
        showTextarea();
      }});
    }} else {{
      status.textContent = "Couldn't copy automatically - the text "
        + "below is selected, copy it manually.";
      showTextarea();
    }}
  }});
}})();
</script>
"""


def _ticker_snapshot_strip_html(ticker, base_url):
    """Compact live-numbers strip above the Part 3 subscribe box (conversion
    pass), shown only on a post with a primary_ticker - Price / IV / MOS /
    Value Score / Moat, read straight off the SAME snapshot_store row every
    other public surface (the /s/<ticker> page, api_v1.py, mcp_server.py)
    already reads via snapshot_store.public_view() (see that function's own
    docstring for the internal->public field mapping), so this can never
    disagree with them - no separate computation. Omitted entirely (empty
    string) when no snapshot has ever been saved for this ticker, same
    "never fabricate a number" rule as every other public snapshot surface."""
    try:
        snap = snapshot_store.get_snapshot(ticker)
    except Exception:
        snap = None
    if not snap:
        return ""
    e = html.escape

    def _fmt(v, suffix=""):
        if v is None:
            return "-"
        return f"{v:g}{suffix}" if isinstance(v, float) else f"{v}{suffix}"

    pub = snapshot_store.public_view(snap.get("data") or {})
    moat = snap.get("moat") or {}
    cells = [
        ("Price", _fmt(pub.get("price"))),
        ("IV", _fmt(pub.get("intrinsic_value"))),
        ("MOS", f"{pub['mos_pct']:+.1f}%" if pub.get("mos_pct") is not None else "-"),
        ("Value Score", _fmt(pub.get("value_score"))),
        ("Moat", _fmt(moat.get("score")) if moat.get("score") is not None else "n/a"),
    ]
    cells_html = "".join(
        '<div style="text-align:center">'
        f'<div style="color:#8aa0b8;font-size:11px;text-transform:uppercase;'
        f'letter-spacing:.4px">{e(label)}</div>'
        f'<div style="color:#e6edf5;font-size:16px;font-weight:700;margin-top:2px">'
        f'{e(str(value))}</div></div>'
        for label, value in cells
    )
    return f"""
<a href="/deep-dive?ticker={e(ticker)}" style="text-decoration:none;color:inherit;
  display:block;margin:0 0 14px">
  <div style="background:#121f36;border:1px solid #1f3352;border-radius:10px;
    padding:14px 18px;display:flex;gap:22px;flex-wrap:wrap;justify-content:space-between;
    align-items:center">
    {cells_html}
    <div style="color:#2dd4bf;font-size:13px;white-space:nowrap">See {e(ticker)}&rsquo;s
      Deep Dive &rarr;</div>
  </div>
</a>
"""


def _blog_subscribe_html(signed_in_email=None, src=None, lang="en"):
    """End-of-post "get the next research note by email" box (conversion
    pass, Part 3). Same underlying mechanism as app.py's
    _render_follow_control/_render_conversion_email_hook (send a code,
    verify inline, one action creates the account) - but this page has no
    Streamlit runtime, so the flow is two small same-origin fetch() calls
    against the new /blog/subscribe/send-code and /blog/subscribe/verify-
    code endpoints in server.py (same _same_origin CSRF gate as the
    comment form and the existing /_auth/* cookie endpoints), and a
    successful verify follows follow_store.ALL_TICKERS ("*") - the same
    sentinel announce_engine already treats as "every research update",
    not one ticker - rather than a specific ticker. The verify response
    hands back a session token, which this script then posts straight to
    the EXISTING /_auth/set-cookie endpoint to complete sign-in in the
    browser, exactly like the rest of the site's cookie flow.

    Hidden entirely for a signed-in visitor already subscribed to the
    general list is wrong on its face - so signed-in shows "You're on the
    list" only when already subscribed, and renders nothing at all
    otherwise (never re-shows the ask box to a visitor who's already
    signed in some other way, matching this batch's Deep Dive hook).

    lang (Español completion, Part 1b): follows the POST's own language
    (post_lang in render_post()) - previously this box had no lang
    parameter at all and rendered English on a Spanish post. `lang` is
    also sent to the two endpoints below so the server-side send-code/
    verify-code messages come back in the same language."""
    import i18n
    if signed_in_email:
        try:
            subscribed = follow_store.is_following(signed_in_email, follow_store.ALL_TICKERS)
        except Exception:
            subscribed = False
        if not subscribed:
            return ""
        return (
            f'<div class="cta" id="sdd-subscribe"><h3>{i18n.t("blog.subscribe.already_heading", lang)}</h3>'
            f'<p>{i18n.t("blog.subscribe.already_body", lang)}</p></div>'
        )

    src_json = json.dumps(src or "")
    lang_json = json.dumps(lang or "en")
    _t = {k: json.dumps(i18n.t(f"blog.subscribe.{k}", lang)) for k in (
        "js_invalid_email", "js_sending", "js_checking",
        "js_network_error", "js_wrong_code_fallback",
    )}
    return f"""
<div class="cta" id="sdd-subscribe">
  <h3>{i18n.t("blog.subscribe.heading", lang)}</h3>
  <div id="sdd-sub-ask">
    <div style="display:flex;gap:10px;flex-wrap:wrap;margin-top:10px">
      <input type="email" id="sdd-sub-email" placeholder="{i18n.t('blog.subscribe.email_placeholder', lang)}"
        style="flex:1 1 220px;background:#0b1220;color:#e6edf5;border:1px solid #1f3352;
        border-radius:8px;padding:10px 12px;font-size:15px;font-family:inherit">
      <button type="button" id="sdd-sub-btn" class="sdd-cite-btn"
        style="padding:10px 20px;font-size:14px">{i18n.t("blog.subscribe.subscribe_button", lang)}</button>
    </div>
    <div id="sdd-sub-status" style="color:#8aa0b8;font-size:12.5px;margin-top:8px"></div>
  </div>
  <div id="sdd-sub-code" style="display:none;margin-top:10px">
    <div style="display:flex;gap:10px;flex-wrap:wrap">
      <input type="text" id="sdd-sub-code-input" maxlength="6" placeholder="{i18n.t('blog.subscribe.code_placeholder', lang)}"
        style="flex:1 1 140px;background:#0b1220;color:#e6edf5;border:1px solid #1f3352;
        border-radius:8px;padding:10px 12px;font-size:15px;font-family:inherit">
      <button type="button" id="sdd-sub-verify-btn" class="sdd-cite-btn"
        style="padding:10px 20px;font-size:14px">{i18n.t("blog.subscribe.verify_button", lang)}</button>
    </div>
    <div id="sdd-sub-code-status" style="color:#8aa0b8;font-size:12.5px;margin-top:8px"></div>
  </div>
  <div id="sdd-sub-done" style="display:none;color:#2dd4bf;margin-top:10px;font-size:15px">
    {i18n.t("blog.subscribe.done", lang)}
  </div>
</div>
<script>
(function(){{
  var askBox = document.getElementById('sdd-sub-ask');
  var codeBox = document.getElementById('sdd-sub-code');
  var doneBox = document.getElementById('sdd-sub-done');
  var emailInput = document.getElementById('sdd-sub-email');
  var status = document.getElementById('sdd-sub-status');
  var codeInput = document.getElementById('sdd-sub-code-input');
  var codeStatus = document.getElementById('sdd-sub-code-status');
  var src = {src_json};
  var lang = {lang_json};
  var sentTo = '';

  document.getElementById('sdd-sub-btn').addEventListener('click', function(){{
    var email = (emailInput.value || '').trim();
    if (!email || email.indexOf('@') === -1) {{
      status.textContent = {_t["js_invalid_email"]};
      return;
    }}
    status.textContent = {_t["js_sending"]};
    fetch('/blog/subscribe/send-code', {{
      method: 'POST', headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{email: email, lang: lang}})
    }}).then(function(r){{ return r.json(); }}).then(function(data){{
      status.textContent = data.message || '';
      if (data.ok) {{
        sentTo = email;
        askBox.style.display = 'none';
        codeBox.style.display = 'block';
      }}
    }}).catch(function(){{
      status.textContent = {_t["js_network_error"]};
    }});
  }});

  document.getElementById('sdd-sub-verify-btn').addEventListener('click', function(){{
    var code = (codeInput.value || '').trim();
    codeStatus.textContent = {_t["js_checking"]};
    fetch('/blog/subscribe/verify-code', {{
      method: 'POST', headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{email: sentTo, code: code, src: src, lang: lang}})
    }}).then(function(r){{ return r.json(); }}).then(function(data){{
      if (data.ok && data.token) {{
        fetch('/_auth/set-cookie', {{
          method: 'POST', headers: {{'Content-Type': 'application/json'}},
          body: JSON.stringify({{tok: data.token}})
        }}).catch(function(){{}}).then(function(){{
          codeBox.style.display = 'none';
          doneBox.style.display = 'block';
        }});
      }} else {{
        codeStatus.textContent = data.message || {_t["js_wrong_code_fallback"]};
      }}
    }}).catch(function(){{
      codeStatus.textContent = {_t["js_network_error"]};
    }});
  }});
}})();
</script>
"""


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
header.site{border-bottom:1px solid #1f3352;padding:16px 0;
  padding-top:calc(16px + env(safe-area-inset-top));
  padding-left:env(safe-area-inset-left);padding-right:env(safe-area-inset-right)}
header.site .wrap{max-width:1080px;display:flex;align-items:center;gap:26px;flex-wrap:wrap}
.brand{font-size:21px;font-weight:800;color:#e6edf5;text-decoration:none}
.brand .accent{color:#2dd4bf}
nav.site{display:flex;align-items:center;gap:20px;flex-wrap:wrap;margin-left:auto}
nav.site a{color:#8aa0b8;font-size:14px}
nav.site a:hover{color:#e6edf5;text-decoration:none}
/* Part 18: small amber "NEW" dot beside the Tools tab, matching
   app.py's own CSS-only ::after dot (same launch-period-only intent -
   NAV_TOOLS_NEW_BADGE_ENABLED above gates whether this class is even
   emitted in the markup). */
nav.site a.nav-tools-new{position:relative}
nav.site a.nav-tools-new::after{content:'';position:absolute;top:-1px;right:-7px;
  width:6px;height:6px;border-radius:50%;background:#f59e0b}
/* Part 5 (site-wide slim nav): the "More" dropdown is a bare
   <details>/<summary> - no JS framework ships in this module, and
   <details> needs none. summary's marker/outline reset + a small
   floating panel is the whole "component".
   3rd Amendment to Part 5: the panel itself is now the SAME
   .sdd-more-panel/.sdd-more-item component app.py's st.popover version
   renders (identical class names/copy - see that module's
   _render_app_nav_items docstring) - "dark card, teal border, shadow,
   ~330px", each item an icon + name + one-line description rather than
   the old bare link list. .nav-more-panel here keeps only the
   ABSOLUTE POSITIONING half of the old rule (top/right/z-index); the
   dark-card/border/shadow/width visuals now live in .sdd-more-panel
   itself, shared verbatim with app.py's <style> block. */
nav.site details.nav-more{position:relative}
nav.site details.nav-more summary{color:#8aa0b8;font-size:14px;cursor:pointer;
  list-style:none;display:flex;align-items:center;gap:4px}
nav.site details.nav-more summary::-webkit-details-marker{display:none}
nav.site details.nav-more summary:hover{color:#e6edf5}
nav.site details.nav-more[open] summary{color:#e6edf5}
nav.site details.nav-more .nav-more-chev{font-size:10px;transition:transform .15s ease}
nav.site details.nav-more[open] .nav-more-chev{transform:rotate(180deg)}
nav.site .nav-more-panel{position:absolute;top:calc(100% + 10px);right:0;z-index:20}
.sdd-more-panel{width:330px;max-width:calc(100vw - 40px);box-sizing:border-box;
  background:#121f36;border:1px solid rgba(45,212,191,.35);border-radius:12px;
  box-shadow:0 16px 40px rgba(0,0,0,.45);padding:8px;display:flex;
  flex-direction:column;gap:2px}
.sdd-more-item{display:flex;align-items:flex-start;gap:12px;padding:10px 12px;
  border-radius:8px;text-decoration:none !important;transition:background .15s ease}
.sdd-more-item:hover{background:rgba(45,212,191,.08);color:#e6edf5;text-decoration:none}
.sdd-more-ic{font-size:18px;line-height:1.3;flex-shrink:0;width:22px;text-align:center}
.sdd-more-txt{display:flex;flex-direction:column;gap:2px;min-width:0}
.sdd-more-name{color:#e6edf5;font-weight:600;font-size:13.5px}
.sdd-more-desc{color:#8aa0b8;font-size:12px;line-height:1.4;white-space:normal}
nav.site .nav-lang{color:#5b7290;font-size:13px;display:flex;align-items:center;gap:6px}
nav.site .nav-lang a{color:#8aa0b8;display:inline-flex;align-items:center;gap:4px}
nav.site .nav-lang a.active{color:#2dd4bf;font-weight:700}
nav.site .nav-lang a:hover{color:#e6edf5}
nav.site .nav-lang .flag{width:16px;height:11px;border-radius:2px;flex-shrink:0;display:block}
nav.site a.nav-signin{color:#2dd4bf;font-weight:600;border:1.5px solid #2dd4bf;
  border-radius:8px;padding:5px 12px;font-size:13.5px}
nav.site a.nav-signin:hover{background:#2dd4bf;color:#0b1220;text-decoration:none}
/* Hidden by default (desktop) - only the @media block below (<=768px)
   flips this to display:flex. Without an unconditional rule here, a
   bare <nav> falls back to the browser's default display:block outside
   the media query instead of actually disappearing. */
.sdd-mnav-bottom{display:none}
/* Owner review round fix #8: mobile app-style bottom icon bar ("Option
   C" in mocks/mobile_nav_options_mock.html) - the SAME component
   app.py's _render_mobile_bottom_nav() renders for the Streamlit pages
   (identical class names, so a viewer landing on a blog post from a
   Deep Dive link, or the other way round, sees an unbroken nav). Hidden
   above 768px; nav.site (the desktop link row) hides below it instead. */
@media(max-width:768px){
  /* Top row per the mock: logo + EN/ES toggle + Sign in, one line - the
     primary link row (Deep Dive..Blog) and the "More" dropdown fold into
     the bottom bar instead, but nav-lang/nav-signin (already the last
     two children of nav.site) stay, right next to the logo. */
  nav.site>a:not(.nav-signin){display:none}
  nav.site>details.nav-more{display:none}
  nav.site{gap:10px}
  /* Fix #8b: same reasoning as app.py's own block-container padding -
     the bar's padding-bottom:env(safe-area-inset-bottom) two lines down
     needs matching room here too, or the last bit of page content ends
     up under the bar on notched iPhones. This page already had
     viewport-fit=cover (see _header_html above), so env() was already
     live here - this was a real, if small, latent gap. */
  body{padding-bottom:calc(78px + env(safe-area-inset-bottom))}
  .sdd-mnav-bottom{position:fixed;left:0;right:0;bottom:0;z-index:9999;
    display:flex;background:#0e1930;border-top:1px solid #1f3352;
    padding-bottom:env(safe-area-inset-bottom)}
  .sdd-mnav-item,.sdd-mnav-more>summary{flex:1;display:flex;flex-direction:column;
    align-items:center;justify-content:center;gap:2px;padding:7px 0 8px;
    font-size:9.5px;color:#8aa0b8 !important;text-decoration:none !important;
    cursor:pointer;list-style:none;user-select:none;position:relative;z-index:2}
  .sdd-mnav-item::-webkit-details-marker,
  .sdd-mnav-more>summary::-webkit-details-marker{display:none}
  .sdd-mnav-item .ic,.sdd-mnav-more>summary .ic{font-size:17px;line-height:1}
  .sdd-mnav-item.on,.sdd-mnav-more[open]>summary{color:#2dd4bf !important}
  .sdd-mnav-more{flex:1}
  .sdd-mnav-dot{position:relative}
  .sdd-mnav-dot::after{content:'';position:absolute;top:1px;right:calc(50% - 15px);
    width:6px;height:6px;border-radius:50%;background:#f59e0b}
  .sdd-mnav-sheet{position:fixed;left:0;right:0;bottom:0;z-index:1;
    background:#0b1220cc;backdrop-filter:blur(2px);
    padding:14px 14px calc(76px + env(safe-area-inset-bottom));
    max-height:75vh;overflow-y:auto}
  .sdd-mnav-sheet .sdd-mnav-sheet-title{font-size:11px;letter-spacing:1px;
    color:#5b7290;text-transform:uppercase;margin:2px 2px 8px}
  .sdd-mnav-sheet .sdd-more-panel{margin:0 auto}
}
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
/* Part 21 (owner-approved mock): the /about bio card - a bordered card
   (all four sides, not the .cta box's left-accent-only treatment, so a
   reader never mistakes the two) sitting above the page's own untouched
   <h1>/prose. */
.sdd-bio-card{background:#121f36;border:1px solid rgba(45,212,191,.35);
  border-radius:14px;padding:24px 26px;margin:0 0 34px}
.sdd-bio-top{display:flex;align-items:center;gap:18px;flex-wrap:wrap}
.sdd-bio-photo{width:76px;height:76px;border-radius:50%;object-fit:cover;
  flex-shrink:0;border:2px solid rgba(45,212,191,.35)}
.sdd-bio-name{display:flex;align-items:center;gap:9px;flex-wrap:wrap;
  color:#e6edf5;font-weight:800;font-size:19px}
.sdd-bio-linkedin{color:#8aa0b8;display:inline-flex;align-items:center;
  line-height:0}
.sdd-bio-linkedin:hover{color:#2dd4bf}
.sdd-bio-role{color:#8aa0b8;font-size:14.5px;margin-top:4px}
.sdd-bio-card p{margin:18px 0 0;color:#cddaea;font-size:16px;line-height:1.65}
.sdd-bio-chips{display:flex;flex-wrap:wrap;gap:8px;margin-top:18px}
.sdd-bio-chip{background:#0b1220;border:1px solid #1f3352;border-radius:999px;
  padding:5px 13px;font-size:12.5px;color:#8aa0b8}
.sdd-bio-draft{background:#0b1220;border:1px solid #1f3352;border-left:3px solid #f59e0b;
  border-radius:8px;padding:10px 14px;margin-top:18px;font-size:13.5px;color:#b9c9dc}
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
.comments{margin:46px 0 0}
.comments h2{font-size:22px;margin:0 0 18px}
.comment{background:#121f36;border:1px solid #1f3352;border-radius:10px;
  padding:14px 18px;margin-bottom:12px}
.comment .c-meta{color:#8aa0b8;font-size:13px;margin:0 0 6px}
.comment .c-meta b{color:#e6edf5}
.comment .c-body{color:#cddaea;font-size:15px;white-space:pre-wrap;margin:0}
.comment-form{background:#121f36;border:1px solid #1f3352;border-radius:12px;
  padding:22px;margin-top:18px}
.comment-form h3{margin:0 0 12px;font-size:17px}
.comment-form input[type=text],.comment-form textarea{
  width:100%;background:#0b1220;border:1px solid #1f3352;border-radius:8px;
  padding:11px 13px;color:#e6edf5;font-size:15px;font-family:inherit;margin:0 0 10px}
.comment-form textarea{min-height:110px;resize:vertical}
.comment-form input:focus,.comment-form textarea:focus{outline:none;border-color:#2dd4bf}
.comment-form button{background:#2dd4bf;color:#06231f;border:0;border-radius:8px;
  padding:11px 24px;font-size:15px;font-weight:700;cursor:pointer;font-family:inherit}
.comment-form button:hover{filter:brightness(1.08)}
.comment-form .hp{position:absolute;left:-9999px;top:-9999px;width:1px;height:1px;
  opacity:0;overflow:hidden}
.comment-banner{border-radius:10px;padding:12px 16px;margin:0 0 16px;font-size:14.5px}
.comment-banner.ok{background:#0f2d27;border:1px solid #1a4a3f;color:#7ee8cf}
.comment-banner.err{background:#2d1414;border:1px solid #4a1f1f;color:#f3a6a6}
.sdd-cite{margin:10px 0 22px}
.sdd-cite-btn{background:#121f36;border:1px solid #1f3352;border-radius:8px;
  color:#8aa0b8;font-size:12.5px;padding:6px 12px;cursor:pointer;font-family:inherit}
.sdd-cite-btn:hover{color:#e6edf5;border-color:#2dd4bf}
@media(max-width:640px){
  body{font-size:16px}
  h1{font-size:31px}
  h2{font-size:23px}
  nav.site{gap:14px;margin-left:0;width:100%}
  .f-cols{gap:28px}
}
"""


def _head(title, description, canonical, base_url, image=None,
          noindex=False, extra_meta="", json_ld="", hreflang_alternates=None):
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
    self-reference it.

    hreflang_alternates (Español instruction, Part 2): optional list of
    (lang_code, url) pairs - one <link rel="alternate" hreflang="..."> per
    pair, e.g. [("en", ".../about"), ("es", ".../es/about"),
    ("x-default", ".../about")]. Only passed by pages that actually have
    an EN/ES twin (currently the four site_content.py pages); every other
    page omits it and gets no hreflang tags at all, per the instruction's
    "translate incrementally" allowance."""
    e = html.escape
    # og:image is emitted only when there is a real image to point at - a
    # card referencing a 404 renders worse on Twitter/LinkedIn than no card
    # image at all, and the summary card is the honest fallback.
    img = image or os.environ.get("DEFAULT_OG_IMAGE", "").strip() or None
    tags = [
        '<meta charset="utf-8">',
        # viewport-fit=cover (Part 4): without it, iOS ignores
        # env(safe-area-inset-*) entirely in standalone (installed) mode,
        # and header.site's safe-area padding below would be a no-op.
        # Harmless in an ordinary browser tab - the env() value is just 0.
        '<meta name="viewport" content="width=device-width,initial-scale=1,'
        'viewport-fit=cover">',
        f"<title>{e(title)}</title>",
        f'<meta name="description" content="{e(description)}">',
    ]
    if canonical:
        tags.append(f'<link rel="canonical" href="{e(canonical)}">')
    for lang_code, alt_url in (hreflang_alternates or []):
        tags.append(
            f'<link rel="alternate" hreflang="{e(lang_code)}" href="{e(alt_url)}">'
        )
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
        PWA_HEAD_TAGS,
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


# Mega-batch Part 5 (site-wide slim nav): the Streamlit tool pages that
# have no /es/-prefixed twin route at all - their Spanish version is the
# live Streamlit app itself in lang=es session state (server.py's
# _STREAMLIT_ONLY_PARAMS forces the catch-all proxy whenever a "lang"
# query param is present). "/" is included too: render_home() has no
# lang param of its own (always English SEO copy), so its Spanish
# version is likewise only reachable by forcing the Streamlit app.
_TOOL_LANG_PARAM_PATHS = {"/", "/deep-dive", "/comparison", "/scanner", "/research", "/portfolio",
                          "/tools"}


def _lang_toggle_links(path):
    """Mega-batch Part 5 (site-wide slim nav): the header's new EN|ES
    toggle needs the CURRENT page's own sibling in the other language,
    not a generic site-wide link - a visitor reading /about in English
    who taps "ES" should land on /es/about, not be bounced to the Spanish
    home page. Rather than inventing a fourth translation scheme, this
    reuses the three that already exist on the site:

      1. A Streamlit tool page (_TOOL_LANG_PARAM_PATHS above) - Spanish
         is the same URL plus ?lang=es, which forces the live Streamlit
         app into its lang=es session state.
      2. A real /es/<path> twin (the site_content.py pages - about,
         methodology, privacy, how-we-use-ai - plus /api, /ai, /blog,
         /calendar, /track-record) - Spanish is literally "/es" + path.
      3. Already on an /es/... path - English is just that path with the
         "/es" prefix removed.

    Returns (en_url, es_url). path=None (a caller with no real per-page
    sibling to offer - render_home, render_not_found, a blog post
    without a published translation) falls back to "/" / "/?lang=es",
    which at least always points somewhere real."""
    if not path:
        return "/", "/?lang=es"
    if path == "/es" or path.startswith("/es/"):
        _en = path[3:] or "/"
        return _en, path
    if path in _TOOL_LANG_PARAM_PATHS:
        return path, f"{path}?lang=es"
    return path, f"/es{path}"


def _header_html(lang="en", path=None, lang_urls=None):
    """Mega-batch Part 5 (site-wide slim nav, Option B): the ONE nav
    every server-rendered page on the site shares - home, blog, the
    static content pages (methodology/about/privacy/how-we-use-ai), the
    four tool-landing SEO pages, track-record and calendar - since every
    one of them is glued together through _page() calling this same
    function (see this module's _page() docstring). Item set/order
    matches _render_app_nav_items() in app.py exactly (that function's
    own docstring has the full rationale): Deep Dive, Research, Scanner,
    Compare, Portfolio, Tools (Part 18 - own tab, own launch-period "NEW"
    dot, never in the More panel), Blog, then a "More" dropdown holding,
    per the 3rd Amendment to Part 5's own verbatim order, Results
    Calendar, Track record, Methodology and About, then the EN|ES toggle
    and a plain Sign in link.

    The "More" dropdown is a bare <details>/<summary> element (styled in
    _CSS below) rather than a JS-built one - this module ships no JS
    framework for a real dropdown component, and <details> needs none.
    Its panel is the SAME .sdd-more-panel/.sdd-more-item component
    app.py's st.popover version renders (3rd Amendment to Part 5): each
    item an icon + bold name + one-line grey description, not a bare
    link list. Click-again-closes is <details>'s own native toggle
    behaviour; click-outside-closes is the one small vanilla-JS listener
    appended right after </header> below (_nav_more_close_script) -
    still no framework, just a document click listener that closes any
    open .nav-more whose bounds don't contain the click.

    Sign in (mega-batch Part 5 scope decision): a server-rendered page
    has no visibility into the Streamlit app's own session/login state
    (they're different processes), so this is always a plain link to
    /portfolio - the sign-in prompt itself lives there - rather than a
    dynamic Sign in/out toggle. A user who is in fact already signed in
    just lands straight on their portfolio, which is a fine outcome
    either way.

    lang="es" translates every label and, per _lang_toggle_links(),
    points Deep Dive/Scanner/Research/Compare/Portfolio at their
    Streamlit ?lang=es forcing param (Research now included - Part 4 of
    this same batch shipped its full Spanish translation, so the old
    "no Spanish research content yet" caveat this function used to carry
    no longer applies) and Methodology at its real /es/methodology twin.

    lang_urls, when given, overrides the derived pair outright - see
    _page()'s own docstring (render_post's real per-post sibling)."""
    _en_url, _es_url = lang_urls if lang_urls else _lang_toggle_links(path)
    # Part 18: Tools gets its own nav tab between Portfolio and Blog here
    # too - "same tab on the static blog/snapshot page navs so the nav
    # stays identical site-wide" (matches app.py's _render_app_nav_items,
    # last entry in _primary_items so it lands right before Blog there).
    _nav_tools_class = " nav-tools-new" if NAV_TOOLS_NEW_BADGE_ENABLED else ""
    # Owner review round fix #8: same launch-period dot, drawn with the
    # mobile bottom bar's own .sdd-mnav-dot marker (positioned for an
    # icon-above-label item, unlike nav-tools-new's ::after which is
    # tuned for a flat inline text link).
    _mnav_dot_class = " sdd-mnav-dot" if NAV_TOOLS_NEW_BADGE_ENABLED else ""
    # 3rd Amendment to Part 5: click-outside-closes for the <details>
    # dropdown - a plain <details>/<summary> only toggles on clicking its
    # own summary, so a click anywhere else on the page needs this one
    # small vanilla-JS listener to close it back up. Order matches the
    # amendment's own spec verbatim: Results Calendar, Track record,
    # Methodology, About.
    _nav_more_close_script = """
<script>
document.addEventListener('click', function(ev){
  document.querySelectorAll('nav.site details.nav-more[open]').forEach(function(d){
    if(!d.contains(ev.target)) d.open = false;
  });
});
</script>
"""
    if lang == "es":
        return f"""
<header class="site"><div class="wrap">
  <a class="brand" href="/">Stocks<span class="accent">DeepDive</span></a>
  <nav class="site">
    <a href="/deep-dive?lang=es">Deep Dive</a>
    <a href="/research?lang=es">Investigación</a>
    <a href="/scanner?lang=es">Buscador</a>
    <a href="/comparison?lang=es">Comparar</a>
    <a href="/portfolio?lang=es">Cartera</a>
    <a href="/tools?lang=es" class="{_nav_tools_class.strip()}">&#128176; Herramientas de dinero</a>
    <a href="/es/blog">Blog</a>
    <details class="nav-more">
      <summary>Más <span class="nav-more-chev">&#9662;</span></summary>
      <div class="nav-more-panel"><div class="sdd-more-panel">
        <a class="sdd-more-item" href="/es/calendar">
          <span class="sdd-more-ic">&#128197;</span>
          <span class="sdd-more-txt"><span class="sdd-more-name">Calendario de resultados</span>
          <span class="sdd-more-desc">Quién presenta resultados esta semana, con los movimientos de puntuación antes/después.</span></span></a>
        <a class="sdd-more-item" href="/es/track-record">
          <span class="sdd-more-ic">&#128200;</span>
          <span class="sdd-more-txt"><span class="sdd-more-name">Historial</span>
          <span class="sdd-more-desc">Recibos fechados: qué calculó el sitio para cada acción, y cuándo.</span></span></a>
        <a class="sdd-more-item" href="/es/methodology">
          <span class="sdd-more-ic">&#129518;</span>
          <span class="sdd-more-txt"><span class="sdd-more-name">Metodología</span>
          <span class="sdd-more-desc">Cómo se calcula cada puntuación y estimación, dato por dato.</span></span></a>
        <a class="sdd-more-item" href="/es/about">
          <span class="sdd-more-ic">&#128100;</span>
          <span class="sdd-more-txt"><span class="sdd-more-name">Acerca de</span>
          <span class="sdd-more-desc">Quién construye esto y por qué es gratis.</span></span></a>
      </div></div>
    </details>
    <span class="nav-lang">
      <a href="{html.escape(_en_url)}">{_FLAG_GB_SVG}EN</a>&#124;<a href="{html.escape(_es_url)}" class="active">{_FLAG_ES_SVG}ES</a>
    </span>
    <a href="/portfolio?lang=es" class="nav-signin">Iniciar sesión</a>
  </nav>
</div></header>
<nav class="sdd-mnav-bottom">
  <a class="sdd-mnav-item" href="/deep-dive?lang=es"><span class="ic">&#128300;</span>Deep Dive</a>
  <a class="sdd-mnav-item" href="/scanner?lang=es"><span class="ic">&#128270;</span>Buscador</a>
  <a class="sdd-mnav-item" href="/portfolio?lang=es"><span class="ic">&#128188;</span>Cartera</a>
  <a class="sdd-mnav-item{_mnav_dot_class}" href="/tools?lang=es"><span class="ic">&#128176;</span>Herramientas</a>
  <details class="sdd-mnav-more">
    <summary><span class="ic">&#9776;</span>Más</summary>
    <div class="sdd-mnav-sheet">
      <div class="sdd-mnav-sheet-title">Más</div>
      <div class="sdd-more-panel">
        <a class="sdd-more-item" href="/research?lang=es">
          <span class="sdd-more-ic">&#128218;</span>
          <span class="sdd-more-txt"><span class="sdd-more-name">Investigación</span>
          <span class="sdd-more-desc">Una empresa a la vez, con la tesis y el veredicto completos.</span></span></a>
        <a class="sdd-more-item" href="/comparison?lang=es">
          <span class="sdd-more-ic">&#9878;&#65039;</span>
          <span class="sdd-more-txt"><span class="sdd-more-name">Comparar</span>
          <span class="sdd-more-desc">Dos o más acciones, alineadas sobre los mismos cálculos.</span></span></a>
        <a class="sdd-more-item" href="/es/blog">
          <span class="sdd-more-ic">&#9997;&#65039;</span>
          <span class="sdd-more-txt"><span class="sdd-more-name">Blog</span>
          <span class="sdd-more-desc">Artículos sobre acciones concretas y cómo funcionan los modelos.</span></span></a>
        <a class="sdd-more-item" href="/es/calendar">
          <span class="sdd-more-ic">&#128197;</span>
          <span class="sdd-more-txt"><span class="sdd-more-name">Calendario de resultados</span>
          <span class="sdd-more-desc">Quién presenta resultados esta semana, con los movimientos de puntuación antes/después.</span></span></a>
        <a class="sdd-more-item" href="/es/track-record">
          <span class="sdd-more-ic">&#128200;</span>
          <span class="sdd-more-txt"><span class="sdd-more-name">Historial</span>
          <span class="sdd-more-desc">Recibos fechados: qué calculó el sitio para cada acción, y cuándo.</span></span></a>
        <a class="sdd-more-item" href="/es/methodology">
          <span class="sdd-more-ic">&#129518;</span>
          <span class="sdd-more-txt"><span class="sdd-more-name">Metodología</span>
          <span class="sdd-more-desc">Cómo se calcula cada puntuación y estimación, dato por dato.</span></span></a>
        <a class="sdd-more-item" href="/es/about">
          <span class="sdd-more-ic">&#128100;</span>
          <span class="sdd-more-txt"><span class="sdd-more-name">Acerca de</span>
          <span class="sdd-more-desc">Quién construye esto y por qué es gratis.</span></span></a>
      </div>
    </div>
  </details>
</nav>
{_nav_more_close_script}"""
    return f"""
<header class="site"><div class="wrap">
  <a class="brand" href="/">Stocks<span class="accent">DeepDive</span></a>
  <nav class="site">
    <a href="/deep-dive">Deep Dive</a>
    <a href="/research">Research</a>
    <a href="/scanner">Scanner</a>
    <a href="/comparison">Compare</a>
    <a href="/portfolio">Portfolio</a>
    <a href="/tools" class="{_nav_tools_class.strip()}">&#128176; Money Tools</a>
    <a href="/blog">Blog</a>
    <details class="nav-more">
      <summary>More <span class="nav-more-chev">&#9662;</span></summary>
      <div class="nav-more-panel"><div class="sdd-more-panel">
        <a class="sdd-more-item" href="/calendar">
          <span class="sdd-more-ic">&#128197;</span>
          <span class="sdd-more-txt"><span class="sdd-more-name">Results Calendar</span>
          <span class="sdd-more-desc">Who reports this week, with before/after score moves.</span></span></a>
        <a class="sdd-more-item" href="/track-record">
          <span class="sdd-more-ic">&#128200;</span>
          <span class="sdd-more-txt"><span class="sdd-more-name">Track record</span>
          <span class="sdd-more-desc">Dated receipts: what the site computed for each stock, and when.</span></span></a>
        <a class="sdd-more-item" href="/methodology">
          <span class="sdd-more-ic">&#129518;</span>
          <span class="sdd-more-txt"><span class="sdd-more-name">Methodology</span>
          <span class="sdd-more-desc">How every score and estimate is calculated, input by input.</span></span></a>
        <a class="sdd-more-item" href="/about">
          <span class="sdd-more-ic">&#128100;</span>
          <span class="sdd-more-txt"><span class="sdd-more-name">About</span>
          <span class="sdd-more-desc">Who builds this and why it's free.</span></span></a>
      </div></div>
    </details>
    <span class="nav-lang">
      <a href="{html.escape(_en_url)}" class="active">{_FLAG_GB_SVG}EN</a>&#124;<a href="{html.escape(_es_url)}">{_FLAG_ES_SVG}ES</a>
    </span>
    <a href="/portfolio" class="nav-signin">Sign in</a>
  </nav>
</div></header>
<nav class="sdd-mnav-bottom">
  <a class="sdd-mnav-item" href="/deep-dive"><span class="ic">&#128300;</span>Deep Dive</a>
  <a class="sdd-mnav-item" href="/scanner"><span class="ic">&#128270;</span>Scanner</a>
  <a class="sdd-mnav-item" href="/portfolio"><span class="ic">&#128188;</span>Portfolio</a>
  <a class="sdd-mnav-item{_mnav_dot_class}" href="/tools"><span class="ic">&#128176;</span>Tools</a>
  <details class="sdd-mnav-more">
    <summary><span class="ic">&#9776;</span>More</summary>
    <div class="sdd-mnav-sheet">
      <div class="sdd-mnav-sheet-title">More</div>
      <div class="sdd-more-panel">
        <a class="sdd-more-item" href="/research">
          <span class="sdd-more-ic">&#128218;</span>
          <span class="sdd-more-txt"><span class="sdd-more-name">Research</span>
          <span class="sdd-more-desc">One company at a time, the full thesis and verdict written out.</span></span></a>
        <a class="sdd-more-item" href="/comparison">
          <span class="sdd-more-ic">&#9878;&#65039;</span>
          <span class="sdd-more-txt"><span class="sdd-more-name">Compare</span>
          <span class="sdd-more-desc">Two or more tickers, lined up on identical calculations.</span></span></a>
        <a class="sdd-more-item" href="/blog">
          <span class="sdd-more-ic">&#9997;&#65039;</span>
          <span class="sdd-more-txt"><span class="sdd-more-name">Blog</span>
          <span class="sdd-more-desc">Write-ups on individual stocks and how the models work.</span></span></a>
        <a class="sdd-more-item" href="/calendar">
          <span class="sdd-more-ic">&#128197;</span>
          <span class="sdd-more-txt"><span class="sdd-more-name">Results Calendar</span>
          <span class="sdd-more-desc">Who reports this week, with before/after score moves.</span></span></a>
        <a class="sdd-more-item" href="/track-record">
          <span class="sdd-more-ic">&#128200;</span>
          <span class="sdd-more-txt"><span class="sdd-more-name">Track record</span>
          <span class="sdd-more-desc">Dated receipts: what the site computed for each stock, and when.</span></span></a>
        <a class="sdd-more-item" href="/methodology">
          <span class="sdd-more-ic">&#129518;</span>
          <span class="sdd-more-txt"><span class="sdd-more-name">Methodology</span>
          <span class="sdd-more-desc">How every score and estimate is calculated, input by input.</span></span></a>
        <a class="sdd-more-item" href="/about">
          <span class="sdd-more-ic">&#128100;</span>
          <span class="sdd-more-txt"><span class="sdd-more-name">About</span>
          <span class="sdd-more-desc">Who builds this and why it's free.</span></span></a>
      </div>
    </div>
  </details>
</nav>
{_nav_more_close_script}"""


def _footer_html(lang="en"):
    """Español instruction, Part 2: same lang-aware treatment as
    _header_html() above, plus the standing disclaimer (i18n.DISCLAIMER_ES
    - see that module's docstring for why it isn't duplicated as an EN
    dict entry). The blog has no Spanish version yet (that's a separate,
    per-post translation workflow, not this instruction's scope) - its
    link stays pointed at the English page even in the Spanish footer,
    labelled "(en inglés)" rather than silently presenting an English
    page as if it were the Spanish one. Track record got its own /es/
    twin in Español completion, Part 3 (track_record_render.py), so its
    footer link now points there with a plain translated label like
    every other Spanish link in this footer."""
    import i18n
    disclaimer = i18n.DISCLAIMER_ES if lang == "es" else DISCLAIMER
    if lang == "es":
        return f"""
<footer class="site"><div class="wrap">
  <div class="f-cols">
    <div><h5>StocksDeepDive</h5>
      <a href="/">Inicio</a>
      <a href="/blog">Blog (en inglés)</a>
      <a href="/es/about">Acerca del autor</a>
      <a href="/es/methodology">Cómo funcionan los puntajes</a>
      <a href="/research">Rational Compounder Research</a>
      <a href="/es/track-record">Historial</a>
    </div>
    <div><h5>Herramientas</h5>
      <a href="/deep-dive?lang=es">Deep Dive</a>
      <a href="/comparison?lang=es">Comparación</a>
      <a href="/scanner?lang=es">Buscador de acciones</a>
    </div>
    <div><h5>Contacto</h5>
      <a href="mailto:rationalcompounder@stocksdeepdive.com">rationalcompounder@stocksdeepdive.com</a>
      <a href="/es/privacy">Política de privacidad</a>
      <a href="/es/how-we-use-ai">Cómo este sitio usa la IA</a>
      <a href="/blog/feed.xml">RSS feed</a>
    </div>
  </div>
  <div class="disclaimer">{disclaimer}</div>
</div></footer>
"""
    return f"""
<footer class="site"><div class="wrap">
  <div class="f-cols">
    <div><h5>StocksDeepDive</h5>
      <a href="/">Home</a>
      <a href="/blog">Blog</a>
      <a href="/about">About the author</a>
      <a href="/methodology">How the scores work</a>
      <a href="/research">Rational Compounder Research</a>
      <a href="/track-record">Track record</a>
    </div>
    <div><h5>Tools</h5>
      <a href="/deep-dive">Stock Deep Dive</a>
      <a href="/comparison">Comparison</a>
      <a href="/scanner">Stock Scanner</a>
    </div>
    <div><h5>Contact</h5>
      <a href="mailto:rationalcompounder@stocksdeepdive.com">rationalcompounder@stocksdeepdive.com</a>
      <a href="/privacy">Privacy policy</a>
      <a href="/how-we-use-ai">How this site uses AI</a>
      <a href="/blog/feed.xml">RSS feed</a>
    </div>
  </div>
  <div class="disclaimer">{disclaimer}</div>
</div></footer>
"""


def _page(head, body, lang="en", path=None, lang_urls=None):
    """lang="es" (Español instruction, Part 2) sets <html lang="es"> and
    renders the Spanish header/footer chrome - every existing caller
    omits it and gets the exact English page as before.

    path (mega-batch Part 5, site-wide slim nav): this page's own path
    (no query string, e.g. "/about" or "/es/about"), passed through to
    _header_html() so its EN|ES toggle can link to the RIGHT sibling page
    instead of a generic site-wide default - see _lang_toggle_links()'s
    own docstring for the exact per-path rules. Optional and omitted by
    several existing callers (render_home/render_not_found have no real
    ES sibling at all) - the toggle falls back to "/" / "/?lang=es" when
    path is None, same as before this parameter existed.

    lang_urls: an explicit (en_url, es_url) pair, used instead of
    deriving one from `path` - render_post's own EN/ES sibling pair
    (already computed there for hreflang, and not a shape
    _lang_toggle_links() can derive - a post's Spanish twin lives at its
    own unrelated slug, not at "/es" + this post's path) is the one
    caller that needs this."""
    return (f"<!doctype html>\n<html lang=\"{lang}\">\n<head>\n{head}\n</head>\n"
            f"<body>\n{_header_html(lang, path, lang_urls)}\n{body}\n{_footer_html(lang)}\n</body>\n</html>")


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


def _person_json_ld(name=None, honorific_suffix=None, same_as=None, job_title=None):
    """honorific_suffix/same_as/job_title (Part 21): the /about page's
    only extra facts about the SAME real Person every other JSON-LD graph
    on the site already names via AUTHOR_NAME - optional and additive, so
    every existing caller (blog post author bylines, the Organization's
    founder, every other content page) keeps emitting the exact same
    bare {"@type": "Person", "name": ...} it always has. same_as is only
    ever passed as ABOUT_LINKEDIN_URL, and only from the /about page."""
    out = {"@type": "Person", "name": name or AUTHOR_NAME}
    if honorific_suffix:
        out["honorificSuffix"] = honorific_suffix
    if job_title:
        out["jobTitle"] = job_title
    if same_as:
        out["sameAs"] = same_as
    return out


def _organization_json_ld(base_url):
    """The site's Organization block, complete (name/url/email/founder) -
    every JSON-LD graph below embeds this SAME dict rather than a
    page-specific partial copy, so "author/organisation schema on every
    page/post" (Phase 4) means every page agrees, not eight slightly
    different Organization objects that happen to share a name."""
    return {
        "@type": "Organization",
        "name": SITE_NAME,
        "url": base_url,
        "email": "rationalcompounder@stocksdeepdive.com",
        "founder": _person_json_ld(),
    }


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
        "author": _person_json_ld(post.get("author") or None),
        "publisher": _organization_json_ld(base_url),
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
        "publisher": _organization_json_ld(base_url),
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


INDEX_TITLE_ES = f"Blog - notas de investigación sobre value investing | {SITE_NAME}"
INDEX_DESC_ES = ("Notas de investigación, análisis de valoración y explicaciones "
                 "de método de StocksDeepDive - cómo se calculan realmente los "
                 "números detrás del veredicto de cada acción.")


def render_index(posts, base_url, page_title=None, description=None,
                 tag=None, noindex=False, lang="en"):
    """lang="es" (Español instruction, Part 3) shows the SAME set of posts -
    the ES blog index is one list, not a filtered one, since we never hide
    an EN post just because it has no Spanish translation yet - but sorted
    with ES-language posts first, and every EN post gets an "(in English)"
    label. lang="en" (the default) renders byte-identical to before this
    param existed."""
    e = html.escape
    is_es = (lang == "es")
    base_path = "/es/blog" if is_es else "/blog"
    canonical = f"{base_url}{base_path}" + (f"?tag={tag}" if tag else "")
    title = page_title or (INDEX_TITLE_ES if is_es else INDEX_TITLE)
    desc = description or (INDEX_DESC_ES if is_es else INDEX_DESC)

    if is_es:
        # Stable sort: ES posts first, EN posts after, each group keeping
        # its existing (already-chronological) relative order.
        ordered = sorted(posts, key=lambda p: 0 if (p.get("lang") or "en") == "es" else 1)
    else:
        ordered = posts

    cards = []
    for p in ordered:
        meta_bits = []
        if p.get("published_at"):
            meta_bits.append(_human_date(p["published_at"]))
        meta_bits.append(f"{reading_time(p.get('body_md'))} min read")
        if p.get("status") != blog_store.STATUS_PUBLISHED:
            meta_bits.append("DRAFT")
        if is_es and (p.get("lang") or "en") != "es":
            meta_bits.append("en inglés")
        # Each post lives at one address - /blog/{slug} - regardless of
        # which index (EN or ES) links to it; posts are separate rows per
        # language (see blog_store.py's translation_of), not one page
        # rendered twice like the static content pages are.
        cards.append(f"""
  <a class="card" href="/blog/{e(p['slug'])}">
    <h2>{e(p['title'])}</h2>
    <p>{e(post_description(p))}</p>
    <div class="meta">{e(' · '.join(meta_bits))}</div>
  </a>""")

    if not cards:
        cards.append(
            '<div class="empty">Todavía no hay publicaciones - la primera '
            'está en camino.</div>' if is_es else
            '<div class="empty">No posts published yet - '
            'the first one is on its way.</div>')

    tag_links = ""
    all_tags = blog_store.all_tags()
    if all_tags:
        _all_label = "Todo" if is_es else "All"
        links = [f'<a class="tag" href="{base_path}">{_all_label}</a>'] + [
            f'<a class="tag" href="{base_path}?tag={e(t)}">{e(t)} ({n})</a>'
            for t, n in all_tags[:12]
        ]
        tag_links = f'<div class="tags" style="margin:0 0 26px">{"".join(links)}</div>'

    if is_es:
        heading = f"Publicaciones etiquetadas “{e(tag)}”" if tag else "Blog"
        body = f"""
<main><div class="wrap">
  <div class="kicker">StocksDeepDive</div>
  <h1>{heading}</h1>
  <p class="lede">Notas de investigación y análisis de valoración - el
  razonamiento detrás de los números que calcula el sitio, explicado en
  detalle. Las publicaciones marcadas “en inglés” aún no tienen traducción
  al español.</p>
  {tag_links}
  {''.join(cards)}
  <div class="cta">
    <h3>Comprueba los números tú mismo</h3>
    <p>Cada cifra que se menciona aquí viene del mismo motor que puedes usar
    con cualquier ticker &mdash; <a href="/deep-dive?lang=es">Stock Deep
    Dive</a>, <a href="/comparison?lang=es">Comparación</a> o el
    <a href="/scanner?lang=es">Escáner de acciones</a>.</p>
  </div>
</div></main>
"""
    else:
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
    # The two indexes are genuine twins of each other (same posts, just
    # reordered/labeled) - not gated on tag pages, since a tag filter on
    # one side has the same filter available on the other.
    hreflang_alternates = [
        ("en", f"{base_url}/blog" + (f"?tag={tag}" if tag else "")),
        ("es", f"{base_url}/es/blog" + (f"?tag={tag}" if tag else "")),
    ]
    head = _head(title, desc, canonical, base_url, noindex=noindex,
                 json_ld=_index_json_ld(posts, base_url),
                 hreflang_alternates=hreflang_alternates)
    return _page(head, body, lang=lang, path=("/es/blog" if lang == "es" else "/blog"))


# -----------------------------------
# TICKER AUTO-LINKING (P3.3a)
#
# Conservative on purpose: .AX tickers are linked wherever they appear in
# prose (the pattern is specific enough not to false-positive), while a
# bare US symbol is only linked when it's already written inside a
# markdown code span (backticks -> <code>...</code>) AND is 2-5 uppercase
# letters with nothing else in the span - so `AAPL` links but `THE` (in
# stray all-caps code text) or a partial match inside a longer code
# string does not. Existing <a>...</a> spans and whole headings are
# protected first so nothing gets double-linked and no heading is
# touched.
# -----------------------------------
_TICKER_AX_RE = re.compile(r"\b([A-Z]{1,5}\.AX)\b")
_TICKER_CODE_RE = re.compile(r"(<code>)([A-Z]{2,5})(</code>)")
_PROTECT_RE = re.compile(r"<a\b[^>]*>.*?</a>|<h[1-6][^>]*>.*?</h[1-6]>",
                         re.S | re.I)


def _autolink_tickers(html_body):
    if not html_body:
        return html_body
    protected = []

    def _stash(m):
        protected.append(m.group(0))
        return f"\x00PROTECTED{len(protected) - 1}\x00"

    out = _PROTECT_RE.sub(_stash, html_body)
    out = _TICKER_AX_RE.sub(
        lambda m: f'<a href="/deep-dive?ticker={m.group(1)}">{m.group(1)}</a>', out
    )
    out = _TICKER_CODE_RE.sub(
        lambda m: (f'{m.group(1)}<a href="/deep-dive?ticker={m.group(2)}">'
                   f'{m.group(2)}</a>{m.group(3)}'),
        out,
    )
    for i, original in enumerate(protected):
        out = out.replace(f"\x00PROTECTED{i}\x00", original)
    return out


def _covered_tickers():
    """The set of tickers with hand-built Rational Compounder research -
    read straight from compounder_data.json (same file app.py's own
    _load_compounder_data() reads), no Streamlit dependency needed since
    it's a plain JSON file on disk/volume."""
    try:
        import build_compounder_data
        path = os.path.join(build_compounder_data._cp_data_dir(),
                            "compounder_data.json")
        if not os.path.exists(path):
            path = os.path.join(os.path.dirname(__file__), "compounder_data.json")
        if not os.path.exists(path):
            return set()
        with open(path) as f:
            data = json.load(f)
        return {t.strip().upper() for t in (data.get("tickers") or {}).keys()}
    except Exception:
        return set()


def _ticker_has_research(ticker):
    return (ticker or "").strip().upper() in _covered_tickers()


def _comments_section_html(slug, comments, comment_status=None, comment_msg=None):
    e = html.escape
    comments = comments or []
    items = []
    for c in comments:
        name = e(c.get("author_name") or "Anonymous")
        date = _human_date(c.get("created_at"))
        body = e(c.get("body") or "")
        items.append(
            f'<div class="comment"><div class="c-meta"><b>{name}</b>'
            f' &middot; {date}</div><p class="c-body">{body}</p></div>'
        )
    banner = ""
    if comment_status == "thanks":
        banner = ('<div class="comment-banner ok">Thanks - your comment is '
                  'in the queue and appears after review.</div>')
    elif comment_status == "error":
        banner = (f'<div class="comment-banner err">{e(comment_msg or "Something went wrong.")}</div>')
    return f"""
<div class="comments" id="comments">
  <h2>Comments ({len(items)})</h2>
  {"".join(items) if items else '<p style="color:#8aa0b8;font-size:14.5px">No comments yet - be the first.</p>'}
  <div class="comment-form">
    <h3>Join the discussion</h3>
    {banner}
    <form method="post" action="/blog/{e(slug)}/comments#comments">
      <input type="text" name="name" placeholder="Name (optional)" maxlength="80">
      <textarea name="body" placeholder="Comment" required maxlength="2000"></textarea>
      <input type="text" name="website" class="hp" tabindex="-1" autocomplete="off">
      <button type="submit">Post comment</button>
    </form>
    <p style="color:#5b7290;font-size:12.5px;margin:10px 0 0">Comments appear
    after review. No account needed.</p>
  </div>
</div>
"""


def render_post(post, base_url, prev_post=None, next_post=None,
                comments=None, comment_status=None, comment_msg=None,
                signed_in_email=None, src=None):
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

    # Español instruction, Part 3: this post's own language (an EN/ES
    # PAIR of posts, not a query-param toggle like the rest of the
    # site - see blog_store.py's schema note) and its translation
    # sibling, if any.
    post_lang = (post.get("lang") or "en").strip().lower()
    sibling = blog_store.get_translation_sibling(post)
    sibling_published = bool(sibling and sibling.get("status") == blog_store.STATUS_PUBLISHED)

    translation_banner = ""
    if post.get("translation_of") and sibling:
        _src_url = post_url(base_url, sibling["slug"])
        translation_banner = (
            '<div class="cta" style="margin:0 0 26px">'
            + ("Traducción automática del original en inglés &mdash; "
               if post_lang == "es" else
               "Automated translation of the original in Spanish &mdash; ")
            + f'<a href="{e(_src_url)}">'
            + ("ver original</a>" if post_lang == "es" else "see original</a>")
            + "</div>"
        )

    cross_link_html = ""
    if sibling_published and not is_draft:
        _sib_url = post_url(base_url, sibling["slug"])
        _label = ("Leer en español" if sibling.get("lang") == "es"
                  else "Read in English")
        cross_link_html = (
            f'<div class="meta" style="margin:0 0 14px">'
            f'<a href="{e(_sib_url)}">{_label} &rarr;</a></div>'
        )

    # P3.1: ticker mentions in the body link straight to that stock's Deep
    # Dive - see _autolink_tickers()'s own docstring for exactly what does
    # and doesn't get linked.
    body_html = _autolink_tickers(md_to_html(post.get("body_md")))

    # P3.2: a ticker-focused post gets a specific end-CTA instead of the
    # generic "put a ticker in" line - one reader-intent click shouldn't
    # be wasted on a post that's already about one company.
    primary_ticker = (post.get("primary_ticker") or "").strip().upper()
    if primary_ticker:
        _research_link = (
            f'<p><a href="/research?ticker={e(primary_ticker)}">Read the '
            f'hand-built {e(primary_ticker)} research &rarr;</a></p>'
            if _ticker_has_research(primary_ticker) else ""
        )
        cta_html = f"""
  <div class="cta">
    <h3>Check {e(primary_ticker)} against the live numbers</h3>
    <p><a href="/deep-dive?ticker={e(primary_ticker)}">See {e(primary_ticker)}'s
    live numbers &rarr;</a></p>
    {_research_link}
    <p>See <a href="/methodology">how the scores work</a>.</p>
  </div>
"""
    else:
        cta_html = """
  <div class="cta">
    <h3>Check any of this against the live numbers</h3>
    <p>Put a ticker into <a href="/deep-dive">Stock Deep Dive</a> and the same
    valuation, quality and psychology maths described here runs on it &mdash;
    with every input shown next to the score. See
    <a href="/methodology">how the scores work</a>.</p>
  </div>
"""

    comments_html = (
        _comments_section_html(post["slug"], comments, comment_status, comment_msg)
        if not is_draft else ""
    )

    # Conversion pass, Part 3: ticker snapshot strip + subscribe box,
    # after the body/CTA, before the prev/next nav and comments. Neither
    # renders on a draft preview - same gate as citation_html/comments_html
    # just above, since a draft isn't a real, indexed, shareable post yet.
    snapshot_strip_html = ""
    subscribe_html = ""
    if not is_draft:
        if primary_ticker:
            snapshot_strip_html = _ticker_snapshot_strip_html(primary_ticker, base_url)
        subscribe_html = _blog_subscribe_html(signed_in_email=signed_in_email, src=src, lang=post_lang)

    citation_html = ""
    if not is_draft:
        author_name = post.get("author") or AUTHOR_NAME
        cite_date = _human_date(post.get("published_at")) or ""
        citation_text = f'{author_name}, "{post["title"]}," {SITE_NAME}' + (
            f", {cite_date}" if cite_date else "") + f". {url}"
        citation_html = _copy_citation_html(citation_text)

    # Conversion pass, Part 5: Reddit-arrival byline - same helper the
    # Deep Dive and Research page use (app.py), so wording/handle can
    # never drift between the three surfaces.
    reddit_byline = (
        reddit_byline_html(ticker=primary_ticker or None)
        if not is_draft and reddit_byline_visible(src) else ""
    )

    body = f"""
<main><div class="wrap">
  <article>
    {draft_banner}
    {translation_banner}
    <div class="kicker"><a href="/blog" style="color:#2dd4bf">Blog</a></div>
    <h1>{e(post['title'])}</h1>
    <div class="meta">{e(' · '.join(meta_bits))}</div>
    {cross_link_html}
    {citation_html}
    {reddit_byline}
    {hero}
    {body_html}
    {tags}
  </article>
  {cta_html}
  {snapshot_strip_html}
  {subscribe_html}
  {nav_html}
  {comments_html}
</div></main>
"""
    # Español instruction, Part 3: only a real published EN/ES pair gets
    # hreflang tags - same "only pages with a real twin" rule Part 2 used
    # for the static content pages. A draft or an unpublished/missing
    # sibling means this post is still effectively single-language.
    hreflang_alternates = None
    if sibling_published:
        _en_post = post if post_lang == "en" else sibling
        _es_post = sibling if post_lang == "en" else post
        hreflang_alternates = [
            ("en", post_url(base_url, _en_post["slug"])),
            ("es", post_url(base_url, _es_post["slug"])),
        ]

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
        hreflang_alternates=hreflang_alternates,
    )
    # Part 5's EN|ES header toggle: a real per-post sibling pair when one
    # is published (same pair hreflang_alternates above already carries),
    # else a sane fallback to that language's blog index - a post with no
    # translation has no per-post Spanish URL to send the toggle to.
    _post_lang_urls = (
        (post_url(base_url, _en_post["slug"]), post_url(base_url, _es_post["slug"]))
        if sibling_published else None
    )
    return _page(head, body, lang=post_lang,
                 path=("/es/blog" if post_lang == "es" else "/blog"),
                 lang_urls=_post_lang_urls)


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
        {"@context": "https://schema.org", **_organization_json_ld(base_url)},
    ])
    head = _head(
        f"{SITE_NAME} - what a stock is worth, with every input shown",
        description, base_url + "/", base_url, json_ld=json_ld,
        extra_meta=f"<style>{_HOME_CSS}</style>",
    )
    return _page(head, body, path="/").replace("<body>", '<body class="home">', 1)


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
        "author": _person_json_ld(),
        "publisher": _organization_json_ld(base_url),
    })
    head = _head(f"{spec['title']} | {SITE_NAME}", spec["description"],
                 canonical, base_url, json_ld=json_ld,
                 extra_meta=f"<style>{_HOME_CSS}</style>")
    return _page(head, body, path=path).replace("<body>", '<body class="home">', 1)


_ABOUT_PATHS = {"/about", "/es/about"}


def _about_bio_card_html(lang):
    """Part 21 (owner-approved mock, small): the bordered bio card at the
    very TOP of /about and /es/about - render_content_page() below
    prepends this ahead of the page's own existing <h1>/prose, which
    stays completely untouched after it (byte-identical, per the
    instruction's own acceptance test).

    Cold-visitor trust-building: a named, credentialled, real engineer
    runs this valuation site, not an anonymous model. Deliberately
    excludes every employer/client name from the owner's CV - "leads a
    bridges engineering team in Brisbane" is the agreed-on wording that
    covers 24 years of bridge work across 4 continents without naming
    who any of it was for.

    lang="es" swaps in a machine-translation draft of the role line,
    both paragraphs and the chips, and adds the same
    "Traduccion automatica ... ver original" style banner already used
    for translated blog posts (see _page_body's translation_banner) -
    the owner reviews this draft before the label comes off, per the
    site's own translation rules. The two English paragraphs below are
    the owner-approved wording, used verbatim."""
    photo_html = ""
    if ABOUT_PHOTO_PATH:
        photo_html = (f'<img src="{html.escape(ABOUT_PHOTO_PATH)}" class="sdd-bio-photo" '
                      f'alt="Andres Moreno">')
    linkedin_html = (f'<a class="sdd-bio-linkedin" href="{html.escape(ABOUT_LINKEDIN_URL)}" '
                      f'target="_blank" rel="noopener" '
                      f'aria-label="Andres Moreno on LinkedIn">{_LINKEDIN_ICON_SVG}</a>')
    chips_en = ["CPEng Chartered &mdash; Engineers Australia",
                "RPEQ Registered Professional Engineer QLD",
                "NER National Engineering Register",
                "24 yrs engineering &middot; 4 continents"]
    chips_es = ["CPEng Acreditado &mdash; Engineers Australia",
                "RPEQ Ingeniero Profesional Registrado (QLD)",
                "NER Registro Nacional de Ingeniería",
                "24 años de ingeniería &middot; 4 continentes"]
    if lang == "es":
        name_line = "Andrés Moreno, BEng, MEng"
        role_line = ("Ingeniero acreditado (Chartered) &middot; inversor particular "
                     "&middot; Brisbane, Australia")
        p1 = ("He pasado <strong>24 años como ingeniero civil y estructural</strong> "
              "diseñando y verificando puentes en Europa, Asia, EE. UU. y Australia "
              "— hoy dirijo un equipo de ingeniería de puentes en Brisbane. La "
              "ingeniería de puentes es una disciplina donde los números tienen que "
              "ser correctos: cada carga se calcula a partir de datos declarados, cada "
              "suposición queda por escrito, y alguien independiente revisa el trabajo "
              "antes de que nadie cruce por él.")
        p2 = ("<strong>Este sitio aplica esa misma disciplina a las acciones.</strong> "
              "Durante años ejecuté estos modelos de forma privada para mi propia "
              "cartera y mi fondo de pensión autogestionado — un motor de DCF, "
              "pruebas de calidad, un cuaderno de trabajo que interroga a una empresa "
              "durante semanas. StocksDeepDive es ese motor abierto al público: cada "
              "dato declarado, cada estimación marcada en rojo, y el juicio siempre "
              "en tus manos.")
        chips = chips_es
        draft_banner = ('<div class="sdd-bio-draft">Traducción automática del '
                        'original en inglés &mdash; <a href="/about">ver original</a>'
                        '</div>')
    else:
        name_line = "Andres Moreno, BEng, MEng"
        role_line = "Chartered engineer &middot; private investor &middot; Brisbane, Australia"
        p1 = ("I&rsquo;ve spent <strong>24 years as a civil &amp; structural engineer</strong> "
              "designing and verifying bridges across Europe, Asia, the USA and Australia "
              "&mdash; today I lead a bridges engineering team in Brisbane. Bridge "
              "engineering is a discipline where the numbers have to be right: every load "
              "is calculated from stated inputs, every assumption is written down, and "
              "someone independent checks the work before anyone drives over it.")
        p2 = ("<strong>This site applies that same discipline to stocks.</strong> For "
              "years I ran these models privately for my own portfolio and self-managed "
              "super fund &mdash; a DCF engine, quality tests, a workbook that interrogates "
              "one company for weeks. StocksDeepDive is that engine opened to the public: "
              "every input stated, every estimate flagged in red, and the judgment always "
              "left with you.")
        chips = chips_en
        draft_banner = ""
    chips_html = "".join(f'<span class="sdd-bio-chip">{c}</span>' for c in chips)
    return f"""<div class="sdd-bio-card">
      <div class="sdd-bio-top">
        {photo_html}
        <div>
          <div class="sdd-bio-name">{name_line}{linkedin_html}</div>
          <div class="sdd-bio-role">{role_line}</div>
        </div>
      </div>
      {draft_banner}
      <p>{p1}</p>
      <p>{p2}</p>
      <div class="sdd-bio-chips">{chips_html}</div>
    </div>"""


def render_content_page(title, markdown_text, description, path, base_url,
                        heading=None, intro_note=None, lang="en",
                        hreflang_alternates=None):
    """A standing content page (How the scores work / About / Privacy) as
    real HTML.

    These pages exist in the Streamlit app too, but a crawler fetching
    them there gets an empty shell. Served from here they are the site's
    only substantive indexable pages besides the blog - and 'how the
    scores work' is the page that explains the whole product, so it is the
    one most worth ranking. The prose comes from site_content.py, the same
    source the app renders, so the two can never drift.

    lang/hreflang_alternates (Español instruction, Part 2): lang picks
    the CTA box's own copy and is passed through to _page() for
    <html lang> and the header/footer chrome; hreflang_alternates goes
    straight to _head(). server.py's content_page() is the only caller
    that passes either right now - every other _page() caller in this
    module still defaults to English-only."""
    note = ""
    if intro_note:
        note = (f'<div class="cta" style="margin:0 0 26px">'
                f'{md_to_html(intro_note)}</div>')
    if lang == "es":
        cta = f"""
  <div class="cta">
    <h3>Míralo funcionar en una empresa real</h3>
    <p>Ingresa un ticker en <a href="/deep-dive?lang=es">Deep Dive</a>, compara dos
    <a href="/comparison?lang=es">lado a lado</a>, o lee la investigación hecha a mano en
    <a href="/research">Rational Compounder</a>. Lo nuevo se publica en
    el <a href="/blog">blog</a> (en inglés).</p>
  </div>"""
    else:
        cta = f"""
  <div class="cta">
    <h3>See it run on a real company</h3>
    <p>Put a ticker into <a href="/deep-dive">Stock Deep Dive</a>, line two up
    <a href="/comparison">side by side</a>, or read the hand-built
    <a href="/research">Rational Compounder research</a>. New writing lands on
    the <a href="/blog">blog</a>.</p>
  </div>"""
    # Part 21: the bio card is a pure PREPEND, only on /about and its ES
    # twin - the h1 and everything from {note} down is untouched, for
    # every page including these two, satisfying the instruction's own
    # "everything currently on the page stays below it, byte-identical".
    bio_card = _about_bio_card_html(lang) if path in _ABOUT_PATHS else ""
    body = f"""
<main><div class="wrap">
  <article>
    {bio_card}
    <h1>{html.escape(heading or title)}</h1>
    {note}
    {md_to_html(markdown_text)}
  </article>
  {cta}
</div></main>
"""
    canonical = f"{base_url}{path}"
    # Part 21: the /about page's Person gets its credentials and LinkedIn
    # sameAs added to the SAME shared _person_json_ld() every other page's
    # author field already uses - see that function's own docstring for
    # why this is additive/optional rather than a change to its default
    # output (every other page keeps emitting the bare Person it always has).
    author_json_ld = (
        _person_json_ld(honorific_suffix="BEng, MEng", same_as=ABOUT_LINKEDIN_URL,
                         job_title="Chartered engineer")
        if path in _ABOUT_PATHS else _person_json_ld()
    )
    json_ld = _json_ld({
        "@context": "https://schema.org",
        "@type": "WebPage",
        "name": title,
        "description": description,
        "url": canonical,
        "isPartOf": {"@type": "WebSite", "name": SITE_NAME, "url": base_url},
        "author": author_json_ld,
        "publisher": _organization_json_ld(base_url),
        # Fix, while touching this code (Español instruction, Part 2):
        # this was a hardcoded "en" regardless of the page actually
        # rendered - a pre-existing inconsistency with _index_json_ld()
        # (which has no inLanguage field at all). Now reflects the real
        # page language; _index_json_ld() itself is untouched in this
        # pass since the blog index has no ES version yet (Part 3).
        "inLanguage": lang,
    })
    head = _head(f"{title} | {SITE_NAME}", description, canonical, base_url,
                 json_ld=json_ld, hreflang_alternates=hreflang_alternates)
    return _page(head, body, lang=lang, path=path)


def render_not_found(base_url, message="That page doesn't exist.", lang="en"):
    """lang (Español completion, Part 3): this generic 404 currently has
    only one caller (server.py's /blog/{slug} unknown-slug case), and the
    blog itself has no per-post Spanish routing scheme yet (/es/blog is a
    reordered listing of the SAME posts, not a translated one - see
    blog_index()'s own docstring) - so there is no live /es/ route that
    reaches this function with lang="es" today. The parameter exists so
    this page is ready the moment one does, without a second signature
    change later; lang="en" (the default) is byte-identical to before."""
    if lang == "es":
        body = f"""
<main><div class="wrap">
  <div class="kicker">404</div>
  <h1>No encontrado</h1>
  <p class="lede">{html.escape(message)}</p>
  <p><a href="/blog">Volver al blog</a> &nbsp;&middot;&nbsp;
     <a href="/">Ir a StocksDeepDive</a></p>
</div></main>
"""
        head = _head("No encontrado | " + SITE_NAME, "Esta página no existe.",
                     None, base_url, noindex=True)
        return _page(head, body, lang=lang)
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
    # Español instruction, Part 2: the four site_content.py pages now have
    # a real /es/ twin - listed right after their EN row, gated by the
    # SAME renders_html(path) check (per content_page()'s own "ES reuses
    # the EN path's INDEXABLE_PAGES gate" decision) so a page that isn't
    # indexable in English doesn't get an indexable-only-in-Spanish
    # sitemap entry either. Kept as plain additional <url> entries rather
    # than xmlns:xhtml sitemap-level hreflang - the in-page <link
    # hreflang> tags (_head()) already carry that signal; doubling it up
    # in the sitemap's own XML namespace was judged not worth the added
    # complexity for four pages.
    _ES_TWIN_PATHS = {"/methodology", "/about", "/how-we-use-ai", "/privacy"}
    for path, priority, freq in APP_PATHS:
        if renders_html is not None and not renders_html(path):
            continue
        rows.append(
            f"  <url><loc>{xml_escape(base_url + path)}</loc>"
            f"<changefreq>{freq}</changefreq>"
            f"<priority>{priority}</priority></url>"
        )
        if path in _ES_TWIN_PATHS:
            rows.append(
                f"  <url><loc>{xml_escape(base_url + '/es' + path)}</loc>"
                f"<changefreq>{freq}</changefreq>"
                f"<priority>{priority}</priority></url>"
            )
    blog_mod = (blog_store.last_modified() or "")[:10] or today
    rows.append(
        f"  <url><loc>{xml_escape(base_url)}/blog</loc>"
        f"<lastmod>{blog_mod}</lastmod>"
        f"<changefreq>weekly</changefreq><priority>0.9</priority></url>"
    )
    # Español instruction, Part 3: /es/blog is a real, always-indexable
    # twin of /blog (same posts, reordered/labelled - see
    # blog_render.render_index()), so it's listed unconditionally just
    # like the EN row above rather than gated by renders_html().
    rows.append(
        f"  <url><loc>{xml_escape(base_url)}/es/blog</loc>"
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


def render_llms_txt(base_url):
    """AI-readiness roadmap Phase 4 (citation helpers): llms.txt - the
    emerging convention (llmstxt.org) for a plain-Markdown index an LLM
    can fetch instead of crawling/rendering the whole site, pointing
    straight at the pages worth reading. No AI key, no computation - a
    static list of the same URLs already in the sitemap, just curated and
    described for a language model rather than exhaustive for a crawler."""
    return f"""# {SITE_NAME}

> Computed stock valuation, quality and psychology scores for ASX and US \
stocks, built and run by {AUTHOR_NAME}, a private investor in Australia. \
Factual data and described calculations only - nothing here is financial \
advice, and every number states the inputs it was computed from.

StocksDeepDive publishes intrinsic value (DCF), quality, crowd-psychology \
and discovery scores for any ASX or US stock, with every input shown, \
plus hand-written value-investing research notes. All computed output is \
free and read-only.

## Tools

- [Stock Deep Dive]({base_url}/deep-dive): one ticker in, a full valuation, \
quality and psychology picture out.
- [Comparison]({base_url}/comparison): the same scores for two or more \
stocks side by side.
- [Stock Scanner]({base_url}/scanner): the ranked overnight scan across a \
whole index.
- [How the scores work]({base_url}/methodology): the calculation behind \
every number, in plain language.

## Data and API

- [Stock snapshots]({base_url}/s/): a plain HTML page per scanned stock, \
updated nightly - start here to read computed scores without a browser \
that runs JavaScript.
- [JSON API]({base_url}/api): free, read-only, no key required - the same \
computed scores as structured data, with attribution and disclaimer \
fields in every response.
- [MCP server]({base_url}/ai): the same data as callable tools for AI \
assistants (endpoint: {base_url}/mcp).
- [Track record]({base_url}/track-record): a dated, past-tense record of \
what the engine computed for each stock and what its price did \
afterwards - not a claim about recommendation accuracy or investment \
performance, see the page itself.

## Research and writing

- [Rational Compounder research]({base_url}/research): hand-built, dated \
research notes on individual companies.
- [Blog]({base_url}/blog): value-investing notes and method explainers.
- [About the author]({base_url}/about): who built and runs this site.

## Terms

Free for any use, commercial or not, with attribution and a link back to \
stocksdeepdive.com. Nothing on this site is financial product advice - \
see {base_url}/methodology and the disclaimer on every page. No user data \
(portfolios, watchlists, emails) is ever published here. Contact: \
rationalcompounder@stocksdeepdive.com.
"""


def render_llms_full_txt(base_url, methodology_md, snapshot_rows, universe_cadence=None):
    """AI-readiness roadmap Phase 4 (citation helpers), Fix 3 (AI fixes
    round 1, 2026-08-31): the llms-full.txt half of the llmstxt.org
    convention. Where render_llms_txt() above is a curated, described
    list of LINKS, llms-full.txt inlines actual content instead - the
    full methodology (every input/weight/assumption every score on the
    site is built from) plus a plain-text index of every ticker
    currently covered by a snapshot, grouped by universe with its direct
    /s/<ticker> link - so a client can fetch this one file and have both
    in hand without a second request per page. `methodology_md` and
    `snapshot_rows` are passed in rather than computed here (same
    separation as every other render_* function in this module - server.
    py owns fetching site_content/snapshot_store data, this module only
    formats it) - `methodology_md` is site_content.methodology_md(...)'s
    return value, `snapshot_rows` is snapshot_store.all_snapshots()'s.

    `universe_cadence` (Fix 8c, AI fixes round 2, 2026-08-31): optional
    {universe: cadence} dict (scheduler_engine._cfg()["universe_cadence"]
    - "daily"/"weekly"/a pinned weekday) - when given, adds a short
    "Coverage & update cadence" section so a client reading this file
    doesn't have to guess which universes refresh nightly vs weekly.
    None (the default) omits the section entirely rather than showing a
    misleading empty one."""
    from collections import OrderedDict
    by_universe = OrderedDict()
    for r in (snapshot_rows or []):
        by_universe.setdefault(r.get("universe") or "Other", []).append(r["ticker"])
    if by_universe:
        index_parts = []
        for universe, tickers in by_universe.items():
            index_parts.append(f"\n### {universe} ({len(tickers)})\n")
            index_parts.extend(f"- {t}: {base_url}/s/{t}" for t in tickers)
        snapshot_index = "\n".join(index_parts)
    else:
        snapshot_index = ("\n(No snapshots yet - the first nightly scan "
                          "will populate this.)\n")

    cadence_section = ""
    if universe_cadence:
        daily = sorted(u for u, c in universe_cadence.items() if c == "daily")
        weekly = sorted(u for u, c in universe_cadence.items() if c != "daily")
        cadence_section = f"""
## Coverage & update cadence

Updated nightly: {', '.join(daily) or '(none)'}.
Updated weekly (one day/week each): {', '.join(weekly) or '(none)'}.
"""

    return f"""# {SITE_NAME} - full reference

> This is the llms-full.txt companion to {base_url}/llms.txt: the same \
site, but with key content inlined here rather than just linked, so one \
fetch covers both the methodology and the current ticker index. See \
{base_url}/llms.txt instead for a shorter, curated page-by-page index.

## Methodology

{methodology_md}
{cadence_section}
## Stock snapshot index

Every ticker StocksDeepDive currently has a computed snapshot for \
({len(snapshot_rows or [])} total), grouped by universe - the same data \
as {base_url}/s/ and {base_url}/api/v1/scan/<universe>, as plain text.
{snapshot_index}

## Terms

Free for any use, commercial or not, with attribution and a link back to \
stocksdeepdive.com. Nothing on this site is financial product advice - \
see {base_url}/methodology and the disclaimer on every page. No user \
data (portfolios, watchlists, emails) is ever published here. Contact: \
rationalcompounder@stocksdeepdive.com.
"""


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
