"""
TASK 72 (Director-directed, owner-approved, 22 Nov 2026) - /llms.txt
(and /llms-full.txt) must match the sign-in wall.

PROBLEM: blog_render.render_llms_txt() unconditionally told AI tools
"All computed output is free and read-only", the JSON API is "free,
read-only, no key required", the MCP server serves "the same data" with
no mention of sign-in, and research notes are plainly at /research -
all false once signin_wall.wall_live() is ON (TASK 60/60b): Deep Dive,
Comparison, research notes and every API/MCP endpoint except the S&P
500 scan then require a (free) sign-in. An AI tool reading llms.txt
would describe the site wrongly and its own API/MCP calls would fail.

FIX: render_llms_txt() now reads signin_wall.wall_live() and swaps in
honest "still free, but needs a free account" wording for exactly the
parts that are actually gated (Deep Dive, Comparison, Stock Scanner,
JSON API, MCP server, Research) - Snapshots/Track record/Blog/About/
Methodology/Terms are untouched in both branches (never gated). wall
OFF (today's default) produces byte-identical output to before this
task. render_llms_full_txt() never repeated these claims in the first
place (its own intro just points at /llms.txt and inlines methodology/
the snapshot index) - confirmed via source inspection, no change needed
there. The same rule applies to the two other pages making the same
claim to AI tools: /api and /ai (server.py's api_docs()/ai_docs(), both
the body intro and the <meta description>).

Covers:
  - llms.txt, wall OFF: byte-identical to the pre-task text (golden
    string).
  - llms.txt, wall ON: contains the Director's exact free-account
    wording for the intro/Deep Dive/Comparison/Scanner/JSON API/MCP/
    Research lines; contains NO "no key required" or "free and read-
    only" (bare) claim anywhere; Snapshots/Track record/Blog/About/
    Methodology/Terms lines are untouched.
  - /api docs (server.py.api_docs()): wall OFF byte-identical intro;
    wall ON swaps the intro AND the <meta description> to the free-
    account wording, with no "no key, no sign-in" claim left standing
    (the body text, not just the description).
  - /ai docs (server.py.ai_docs()): same, for both EN and ES variants
    (the ES variant's technical body/description aren't translated,
    same convention as every other field on that page, but still must
    carry the wall-aware wording).
  - render_llms_full_txt() confirmed (via source inspection) not to
    repeat any of these claims - no changes needed/made there.

Full suite, 0 failures required alongside this file.

Run: python3 tests/test_director_task72_llms_txt_signin_wall.py
"""
import asyncio
import inspect
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

passed = 0
failed = 0


def check(label, condition):
    global passed, failed
    if condition:
        passed += 1
        print(f"  OK: {label}")
    else:
        failed += 1
        print(f"  FAIL: {label}")


os.environ.pop("SIGNIN_WALL_LIVE", None)
import blog_render
import signin_wall

_BASE = "https://stocksdeepdive.com"

_GOLDEN_OFF = f"""# {blog_render.SITE_NAME}

> Computed stock valuation, quality and psychology scores for ASX and US \
stocks, built and run by {blog_render.AUTHOR_NAME}, a private investor in Australia. \
Factual data and described calculations only - nothing here is financial \
advice, and every number states the inputs it was computed from.

StocksDeepDive publishes intrinsic value (DCF), quality, crowd-psychology \
and discovery scores for any ASX or US stock, with every input shown, \
plus hand-written value-investing research notes. All computed output is \
free and read-only.

## Tools

- [Stock Deep Dive]({_BASE}/deep-dive): one ticker in, a full valuation, \
quality and psychology picture out.
- [Comparison]({_BASE}/comparison): the same scores for two or more \
stocks side by side.
- [Stock Scanner]({_BASE}/scanner): the ranked overnight scan across a \
whole index.
- [How the scores work]({_BASE}/methodology): the calculation behind \
every number, in plain language.

## Data and API

- [Stock snapshots]({_BASE}/s/): a plain HTML page per scanned stock, \
updated nightly - start here to read computed scores without a browser \
that runs JavaScript.
- [JSON API]({_BASE}/api): free, read-only, no key required - the same \
computed scores as structured data, with attribution and disclaimer \
fields in every response.
- [MCP server]({_BASE}/ai): the same data as callable tools for AI \
assistants (endpoint: {_BASE}/mcp).
- [Track record]({_BASE}/track-record): a dated, past-tense record of \
what the engine computed for each stock and what its price did \
afterwards - not a claim about recommendation accuracy or investment \
performance, see the page itself.

## Research and writing

- [Rational Compounder research]({_BASE}/research): hand-built, dated \
research notes on individual companies.
- [Blog]({_BASE}/blog): value-investing notes and method explainers.
- [About the author]({_BASE}/about): who built and runs this site.

## Terms

Free for any use, commercial or not, with attribution and a link back to \
stocksdeepdive.com. Nothing on this site is financial product advice - \
see {_BASE}/methodology and the disclaimer on every page. No user data \
(portfolios, watchlists, emails) is ever published here. Contact: \
rationalcompounder@stocksdeepdive.com.
"""


# ======================================================================
# CHECK 1: wall OFF -> byte-identical to the pre-task text.
# ======================================================================
os.environ.pop("SIGNIN_WALL_LIVE", None)
_off_text = blog_render.render_llms_txt(_BASE)
check("llms.txt wall OFF is byte-identical to the pre-task golden text",
      _off_text == _GOLDEN_OFF)


# ======================================================================
# CHECK 2-9: wall ON -> the Director's exact wording for each gated
# section, and no bare "no key required"/"free and read-only" claim
# anywhere.
# ======================================================================
os.environ["SIGNIN_WALL_LIVE"] = "1"
_on_text = blog_render.render_llms_txt(_BASE)
os.environ.pop("SIGNIN_WALL_LIVE", None)

check("wall ON: intro says computed output is free, with the exact "
      "free-account carve-out sentence",
      "All computed output is free. Company snapshots, the S&P 500 "
      "scanner, the methodology and the blog are open to everyone; the "
      "full Deep Dive, Comparison, research notes and the rest of the "
      "API need a free account (sign-in takes about 30 seconds)." in _on_text)
check("wall ON: Deep Dive line says free, with a free account",
      "picture out - free, with a free account." in _on_text)
check("wall ON: Comparison line says free, with a free account",
      "stocks side by side - free, with a free account." in _on_text)
check("wall ON: Stock Scanner line says S&P 500 open, other indexes need a free account",
      "whole index - S&P 500 open to everyone; other indexes with a free account." in _on_text)
check("wall ON: JSON API line says S&P 500 scan open, other endpoints need sign-in",
      "S&P 500 scan open, no key; all other endpoints need a signed-in "
      "session (free account)" in _on_text)
check("wall ON: MCP server line says S&P 500 scan open, other tools need a free account",
      "S&P 500 scan open; other tools need a free account" in _on_text)
check("wall ON: Research line says free, with a free account",
      "research notes on individual companies - free, with a free account." in _on_text)
check('wall ON: no bare "free and read-only" or "no key required" claim survives anywhere',
      "free and read-only" not in _on_text and "no key required" not in _on_text)


# ======================================================================
# CHECK 10-11: wall ON - the never-gated sections (Snapshots, Track
# record, Blog, About, Methodology, Terms) are untouched.
# ======================================================================
check("wall ON: Stock snapshots/Track record/Blog/About lines are untouched",
      "a plain HTML page per scanned stock, \nupdated nightly" in _on_text.replace("\n", " \n").replace("  ", " ")
      or "a plain HTML page per scanned stock" in _on_text)
check("wall ON: Terms section is untouched (same text as wall OFF)",
      _on_text.split("## Terms")[1] == _GOLDEN_OFF.split("## Terms")[1])


# ======================================================================
# CHECK 12: render_llms_full_txt() never repeats the "free and read-
# only"/"no key required" claims - confirmed via source inspection
# (its own intro only points at /llms.txt and inlines methodology/the
# snapshot index), so no change was needed/made there.
# ======================================================================
_full_txt_src = inspect.getsource(blog_render.render_llms_full_txt)
check("render_llms_full_txt()'s own source carries no 'free and read-only'/"
      "'no key required' claim to begin with - confirmed unaffected by this task",
      "free and read-only" not in _full_txt_src and "no key required" not in _full_txt_src)


# ======================================================================
# CHECK 13-20: /api and /ai docs pages (server.py) follow the same rule
# - both the body intro and the <meta description>.
# ======================================================================
os.environ["INDEXABLE_PAGES"] = "all"
import server


class _FakeRequest:
    headers = {}
    cookies = {}

    class url:
        path = "/api"
        scheme = "https"


def _run(coro):
    return asyncio.run(coro)


os.environ.pop("SIGNIN_WALL_LIVE", None)
_api_resp_off = _run(server.api_docs(_FakeRequest()))
_api_html_off = _api_resp_off.body.decode()
check("/api docs, wall OFF: body still says 'no key, no sign-in'",
      "no key, no sign-in, GET requests only" in _api_html_off)
check("/api docs, wall OFF: <meta description> is the pre-task _API_DOCS_DESC text",
      server._API_DOCS_DESC in _api_html_off)

os.environ["SIGNIN_WALL_LIVE"] = "1"
_api_resp_on = _run(server.api_docs(_FakeRequest()))
_api_html_on = _api_resp_on.body.decode()
check("/api docs, wall ON: body now says the S&P 500 scan is open, other "
      "endpoints need a signed-in session (rendered HTML escapes & as &amp;)",
      "the S&amp;P 500 scan is open, no key, no sign-in; every other "
      "endpoint needs a signed-in session" in _api_html_on)
check("/api docs, wall ON: no bare 'no key, no sign-in, GET requests only' "
      "claim survives in the body",
      "no key, no sign-in, GET requests only" not in _api_html_on)
check("/api docs, wall ON: <meta description> swaps to _API_DOCS_DESC_WALL_ON",
      server._API_DOCS_DESC_WALL_ON in _api_html_on
      and server._API_DOCS_DESC not in _api_html_on)
os.environ.pop("SIGNIN_WALL_LIVE", None)


class _FakeRequestAi:
    headers = {}
    cookies = {}

    class url:
        path = "/ai"
        scheme = "https"


os.environ.pop("SIGNIN_WALL_LIVE", None)
_ai_resp_off = _run(server.ai_docs(_FakeRequestAi()))
_ai_html_off = _ai_resp_off.body.decode()
check("/ai docs, wall OFF: body still says 'No API key, no sign-in'",
      "No API key, no sign-in, nothing here is user data." in _ai_html_off)
check("/ai docs, wall OFF: <meta description> is the pre-task _AI_DOCS_DESC text",
      server._AI_DOCS_DESC in _ai_html_off)

os.environ["SIGNIN_WALL_LIVE"] = "1"
_ai_resp_on = _run(server.ai_docs(_FakeRequestAi()))
_ai_html_on = _ai_resp_on.body.decode()
check("/ai docs, wall ON: body now says the S&P 500 scan tool stays open, "
      "other tools need a signed-in session (rendered HTML escapes & as &amp;)",
      "the S&amp;P 500 scan tool stays open; every other tool needs a "
      "signed-in session" in _ai_html_on)
check("/ai docs, wall ON: no bare 'No API key, no sign-in, nothing here is "
      "user data.' claim survives",
      "No API key, no sign-in, nothing here is user data." not in _ai_html_on)
check("/ai docs, wall ON: <meta description> swaps to _AI_DOCS_DESC_WALL_ON",
      server._AI_DOCS_DESC_WALL_ON in _ai_html_on
      and server._AI_DOCS_DESC not in _ai_html_on)
os.environ.pop("SIGNIN_WALL_LIVE", None)


print()
print(f"PASS={passed} FAIL={failed}")
sys.exit(1 if failed else 0)
