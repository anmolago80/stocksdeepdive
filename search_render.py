"""
search_render.py

Server-rendered results page for the nav ticker/company search box -
/search, /es/search. See server.py's search_page() route and
ticker_search_engine.py for the matching logic that decides whether a
query lands here at all: a single confident match redirects straight to
/s/<ticker> (or /es/s/<ticker>) before this module is even called, and a
query with zero matches goes to snapshot_render.render_snapshot_not_found
instead (with "closest matches" suggestions) rather than this page's
empty state. This page therefore only ever renders for a query with
more than one candidate, or no query at all (the bare /search landing).
"""

import html
from urllib.parse import quote

import blog_render
import snapshot_render

SITE_NAME = blog_render.SITE_NAME


def render_search_results(query, matches, base_url, lang="en"):
    e = html.escape
    path = "/es/search" if lang == "es" else "/search"
    canonical = f"{base_url}{path}"
    if query:
        canonical += f"?q={quote(query)}"

    if lang == "es":
        kicker = "Buscar"
        heading = f'Resultados para "{query}"' if query else "Buscar acciones"
        lede = (f"{len(matches)} coincidencia{'s' if len(matches) != 1 else ''} entre las "
                "acciones cubiertas." if query else
                "Escribe un ticker o el nombre de una empresa.")
        th_ticker, th_name, th_universe = "Ticker", "Empresa", "Universo"
        empty_msg = "Sin coincidencias entre las acciones cubiertas." if query else ""
        s_prefix = "/es/s"
    else:
        kicker = "Search"
        heading = f'Results for "{query}"' if query else "Search stocks"
        lede = (f"{len(matches)} match{'es' if len(matches) != 1 else ''} among covered "
                "stocks." if query else "Type a ticker or company name.")
        th_ticker, th_name, th_universe = "Ticker", "Company", "Universe"
        empty_msg = "No matches among covered stocks." if query else ""
        s_prefix = "/s"

    if matches:
        rows = "".join(
            f'<tr><td><a href="{s_prefix}/{e(m["ticker"])}">{e(m["ticker"])}</a></td>'
            f'<td>{e(m.get("company_name") or "-")}</td>'
            f'<td>{e(m.get("universe") or "-")}</td></tr>'
            for m in matches
        )
        table = (f'<table class="sdd-snap-table"><thead><tr>'
                 f'<th>{th_ticker}</th><th>{th_name}</th><th>{th_universe}</th>'
                 f'</tr></thead><tbody>{rows}</tbody></table>')
    elif empty_msg:
        table = f'<div class="empty">{empty_msg}</div>'
    else:
        table = ""

    body = f"""
<main><div class="wrap">
  <div class="kicker">{kicker}</div>
  <h1>{e(heading)}</h1>
  <p class="lede">{e(lede)}</p>
  {table}
</div></main>
"""
    title = f"{heading} | {SITE_NAME}" if query else f"Search | {SITE_NAME}"
    head = blog_render._head(title, lede, canonical, base_url, noindex=True,
                             extra_meta=f"<style>{snapshot_render._grid_css()}</style>")
    return blog_render._page(head, body, lang=lang, path=path)
