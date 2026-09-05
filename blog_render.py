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


def _copy_as_text_html(copy_text, dom_id="sdd-copytext"):
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
    are per-ticker (a Deep Dive page, a /s/<ticker> snapshot page)."""
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
  <button type="button" class="sdd-cite-btn" id="{e(dom_id)}-btn">Copy as text</button>
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


def _blog_subscribe_html(signed_in_email=None, src=None):
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
    signed in some other way, matching this batch's Deep Dive hook)."""
    if signed_in_email:
        try:
            subscribed = follow_store.is_following(signed_in_email, follow_store.ALL_TICKERS)
        except Exception:
            subscribed = False
        if not subscribed:
            return ""
        return (
            '<div class="cta" id="sdd-subscribe"><h3>You\'re on the list.</h3>'
            "<p>You'll get the next research note by email.</p></div>"
        )

    src_json = json.dumps(src or "")
    return f"""
<div class="cta" id="sdd-subscribe">
  <h3>Get the next research note by email &mdash; free.</h3>
  <div id="sdd-sub-ask">
    <div style="display:flex;gap:10px;flex-wrap:wrap;margin-top:10px">
      <input type="email" id="sdd-sub-email" placeholder="you@example.com"
        style="flex:1 1 220px;background:#0b1220;color:#e6edf5;border:1px solid #1f3352;
        border-radius:8px;padding:10px 12px;font-size:15px;font-family:inherit">
      <button type="button" id="sdd-sub-btn" class="sdd-cite-btn"
        style="padding:10px 20px;font-size:14px">Subscribe</button>
    </div>
    <div id="sdd-sub-status" style="color:#8aa0b8;font-size:12.5px;margin-top:8px"></div>
  </div>
  <div id="sdd-sub-code" style="display:none;margin-top:10px">
    <div style="display:flex;gap:10px;flex-wrap:wrap">
      <input type="text" id="sdd-sub-code-input" maxlength="6" placeholder="123456"
        style="flex:1 1 140px;background:#0b1220;color:#e6edf5;border:1px solid #1f3352;
        border-radius:8px;padding:10px 12px;font-size:15px;font-family:inherit">
      <button type="button" id="sdd-sub-verify-btn" class="sdd-cite-btn"
        style="padding:10px 20px;font-size:14px">Verify</button>
    </div>
    <div id="sdd-sub-code-status" style="color:#8aa0b8;font-size:12.5px;margin-top:8px"></div>
  </div>
  <div id="sdd-sub-done" style="display:none;color:#2dd4bf;margin-top:10px;font-size:15px">
    Done &mdash; you're on the list.
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
  var sentTo = '';

  document.getElementById('sdd-sub-btn').addEventListener('click', function(){{
    var email = (emailInput.value || '').trim();
    if (!email || email.indexOf('@') === -1) {{
      status.textContent = "That doesn't look like a valid email address.";
      return;
    }}
    status.textContent = 'Sending...';
    fetch('/blog/subscribe/send-code', {{
      method: 'POST', headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{email: email}})
    }}).then(function(r){{ return r.json(); }}).then(function(data){{
      status.textContent = data.message || '';
      if (data.ok) {{
        sentTo = email;
        askBox.style.display = 'none';
        codeBox.style.display = 'block';
      }}
    }}).catch(function(){{
      status.textContent = "Couldn't reach the server - please try again.";
    }});
  }});

  document.getElementById('sdd-sub-verify-btn').addEventListener('click', function(){{
    var code = (codeInput.value || '').trim();
    codeStatus.textContent = 'Checking...';
    fetch('/blog/subscribe/verify-code', {{
      method: 'POST', headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{email: sentTo, code: code, src: src}})
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
        codeStatus.textContent = data.message || 'Wrong code - try again.';
      }}
    }}).catch(function(){{
      codeStatus.textContent = "Couldn't reach the server - please try again.";
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


def _person_json_ld(name=None):
    return {"@type": "Person", "name": name or AUTHOR_NAME}


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
        subscribe_html = _blog_subscribe_html(signed_in_email=signed_in_email, src=src)

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
    <div class="kicker"><a href="/blog" style="color:#2dd4bf">Blog</a></div>
    <h1>{e(post['title'])}</h1>
    <div class="meta">{e(' · '.join(meta_bits))}</div>
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
        {"@context": "https://schema.org", **_organization_json_ld(base_url)},
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
        "author": _person_json_ld(),
        "publisher": _organization_json_ld(base_url),
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
        "author": _person_json_ld(),
        "publisher": _organization_json_ld(base_url),
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
