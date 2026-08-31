"""
snapshot_render.py

Server-rendered HTML for /s/<TICKER> (one page per scanned stock) and
/s/ (the index) - Phase 1 of the AI-readiness roadmap
(AI_ROADMAP_stocksdeepdive.md). Same reasoning as blog_render.py: a
crawler (or an AI system) fetching a Streamlit URL gets an empty
JavaScript shell, so the numbers behind a ticker need a plain, complete
HTML document of their own. Reuses blog_render's page shell (header,
footer, disclaimer, CSS, JSON-LD helper) rather than duplicating it, so
a snapshot page looks and reads like the rest of the site.

Snapshot pages are read-only reflections of snapshot_store - this module
has no write path and touches no user data; every number here already
appears on the public Scanner/Deep Dive pages.
"""

import html
import re

import blog_render

SITE_NAME = blog_render.SITE_NAME

# Plain-text disclaimer for JSON responses (the API can't send blog_render's
# HTML <b> tags) - derived from the ONE disclaimer string the rest of the
# site already uses, so the wording can never drift between the HTML
# footer and the API payload.
PLAIN_DISCLAIMER = re.sub(r"<[^>]+>", "", blog_render.DISCLAIMER)

ATTRIBUTION = (
    f"Data and computed scores from {SITE_NAME} (stocksdeepdive.com). "
    "Please attribute and link back when quoting or reproducing."
)


def snapshot_url(base_url, ticker):
    return f"{base_url}/s/{ticker}"


# -----------------------------------
# Small helpers over the row shapes nightly_scan.analyze_ticker_lite()
# and moat_engine.compute_moat() return - see their own docstrings for
# the authoritative field list.
# -----------------------------------

def _fmt(value, suffix="", none="-"):
    if value is None:
        return none
    if isinstance(value, float):
        return f"{value:g}{suffix}"
    return f"{value}{suffix}"


def _valuation_note(data):
    val = data.get("Valuation")
    mos = data.get("MOS %")
    if val and val != "N/A" and mos is not None:
        return f"{val} (MOS {mos:g}%)"
    return val or "-"


def _stat_cells(data, moat):
    """[(label, value, hint), ...] for the stat grid - deliberately every
    number the page shows, so nothing here needs any computation of its
    own beyond formatting; the site's engines already computed all of it."""
    cells = [
        ("Price", _fmt(data.get("Price"), suffix=""), None),
        ("Long Score", _fmt(data.get("Long Score")), data.get("Signal")),
        ("Valuation", _valuation_note(data), None),
        ("Quality", _fmt(data.get("Quality")),
         "estimated (no reported figure)" if data.get("Quality Default") else None),
        ("Psychology", _fmt(data.get("Psychology")), None),
        ("Discovery", _fmt(data.get("Discovery (lite)")),
         "price/volume attention only"),
        ("Trend", data.get("Trend") or "-", None),
        ("Trade setup", data.get("Trade Setup") or "-", None),
    ]
    if moat:
        moat_score = moat.get("score")
        erosion = (moat.get("erosion") or "").replace("_", " ")
        cells.append((
            "Moat score",
            _fmt(moat_score) if moat_score is not None else "n/a",
            f"erosion: {erosion}" if erosion else None,
        ))
    return cells


def _grid_css():
    return """
.sdd-snap-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(160px,1fr));
  gap:12px;margin:22px 0 30px}
.sdd-snap-cell{background:#121f36;border:1px solid #1f3352;border-radius:12px;padding:14px 16px}
.sdd-snap-cell .lbl{font-family:ui-monospace,Menlo,monospace;font-size:10.5px;
  letter-spacing:1.1px;text-transform:uppercase;color:#5b7290;margin-bottom:6px}
.sdd-snap-cell .val{font-size:22px;font-weight:700;color:#e6edf5}
.sdd-snap-cell .hint{font-size:12.5px;color:#8aa0b8;margin-top:4px}
.sdd-snap-table{width:100%;border-collapse:collapse;font-size:14.5px;margin:0 0 24px}
.sdd-snap-table th,.sdd-snap-table td{padding:8px 10px;border-bottom:1px solid #1f3352;
  text-align:left}
.sdd-snap-table th{color:#8aa0b8;font-weight:600;font-size:13px}
.sdd-snap-table a{color:#2dd4bf}
"""


def render_snapshot(snap, base_url):
    """snap is snapshot_store.get_snapshot(ticker)'s return value (not
    None - caller handles the unknown-ticker/404 case)."""
    e = html.escape
    ticker = snap["ticker"]
    data = snap.get("data") or {}
    moat = snap.get("moat")
    universe = snap.get("universe") or ""
    generated = (snap.get("generated_at") or "")[:16].replace("T", " ")

    cells = _stat_cells(data, moat)
    cell_html = "".join(
        f'<div class="sdd-snap-cell"><div class="lbl">{e(label)}</div>'
        f'<div class="val">{e(str(value))}</div>'
        + (f'<div class="hint">{e(hint)}</div>' if hint else "")
        + "</div>"
        for label, value, hint in cells
    )

    signal = data.get("Signal") or ""
    valuation = _valuation_note(data)
    lede = (f"{e(signal)} &middot; {e(valuation)} &middot; "
            f"Quality {e(_fmt(data.get('Quality')))}/100" if signal else
            "Computed scores for this stock, from the same engine behind "
            "the site's Deep Dive and Scanner pages.")

    title = f"{ticker} snapshot - {signal or 'computed scores'} | {SITE_NAME}"
    canonical = snapshot_url(base_url, ticker)
    cite_date = (snap.get("generated_at") or "")[:10]
    citation_text = f'{SITE_NAME}, "{ticker} snapshot,"' + (
        f" {cite_date}" if cite_date else "") + f". {canonical}"

    body = f"""
<main><div class="wrap">
  <div class="kicker">Snapshot &middot; {e(universe)} &middot; generated {e(generated)} UTC</div>
  <h1>{e(ticker)}</h1>
  <p class="lede">{lede}</p>
  {blog_render._copy_citation_html(citation_text)}
  <div class="sdd-snap-grid">{cell_html}</div>
  <p><a href="/deep-dive?ticker={e(ticker)}">Open the interactive Deep Dive for {e(ticker)} &rarr;</a></p>
  <div class="cta">
    <h3>Use this data programmatically</h3>
    <p>This page's numbers are also available as JSON at
    <a href="/api/v1/deep-dive/{e(ticker)}">/api/v1/deep-dive/{e(ticker)}</a> -
    see <a href="/api">the API docs</a> for the full read-only surface,
    including an MCP server for AI assistants.</p>
  </div>
</div></main>
"""
    description = (
        f"{ticker}: {valuation}, Long Score {_fmt(data.get('Long Score'))}, "
        f"Quality {_fmt(data.get('Quality'))}/100 - computed scores from "
        f"{SITE_NAME}'s Deep Dive engine, updated nightly."
    )
    json_ld = blog_render._json_ld({
        "@context": "https://schema.org",
        "@type": "WebPage",
        "name": title,
        "description": description,
        "url": canonical,
        "isPartOf": {"@type": "WebSite", "name": SITE_NAME, "url": base_url},
        "publisher": blog_render._organization_json_ld(base_url),
        "about": {"@type": "Corporation", "name": ticker},
        "dateModified": (snap.get("generated_at") or "")[:19],
        "inLanguage": "en",
    })
    head = blog_render._head(
        title, description, canonical, base_url,
        extra_meta=f"<style>{_grid_css()}</style>", json_ld=json_ld,
    )
    return blog_render._page(head, body)


def render_snapshot_not_found(base_url, ticker):
    body = f"""
<main><div class="wrap">
  <div class="kicker">404</div>
  <h1>No snapshot for {html.escape(ticker)}</h1>
  <p class="lede">This ticker hasn't been through the nightly scan yet -
  it may not be in a covered universe, or a scan just hasn't run for it.</p>
  <p><a href="/s/">Browse covered stocks</a> &nbsp;&middot;&nbsp;
     <a href="/deep-dive?ticker={html.escape(ticker)}">Try the live Deep Dive for {html.escape(ticker)} instead &rarr;</a></p>
</div></main>
"""
    head = blog_render._head(
        f"No snapshot for {ticker} | {SITE_NAME}",
        "This ticker has not been scanned yet.",
        f"{base_url}/s/", base_url, noindex=True,
    )
    return blog_render._page(head, body)


def render_index(rows, base_url, page=1, per_page=200):
    """rows: snapshot_store.all_snapshots() output (ticker/universe/
    generated_at only - no per-row scoring, so this stays cheap even at
    a few hundred tickers)."""
    e = html.escape
    total = len(rows)
    start = (page - 1) * per_page
    page_rows = rows[start:start + per_page]

    if page_rows:
        trs = "".join(
            f'<tr><td><a href="/s/{e(r["ticker"])}">{e(r["ticker"])}</a></td>'
            f'<td>{e(r["universe"])}</td>'
            f'<td>{e((r.get("generated_at") or "")[:10])}</td></tr>'
            for r in page_rows
        )
        table = (f'<table class="sdd-snap-table"><thead><tr>'
                 f'<th>Ticker</th><th>Universe</th><th>Updated</th>'
                 f'</tr></thead><tbody>{trs}</tbody></table>')
    else:
        table = ('<div class="empty">No snapshots yet - the first nightly '
                 'scan will populate this page.</div>')

    body = f"""
<main><div class="wrap">
  <div class="kicker">StocksDeepDive</div>
  <h1>Stock snapshots</h1>
  <p class="lede">{total} covered stock{'s' if total != 1 else ''}, nightly-computed
  and updated automatically - the same numbers behind the interactive
  <a href="/deep-dive">Deep Dive</a> and <a href="/scanner">Scanner</a> pages,
  as a plain page per ticker. See <a href="/api">the API</a> for JSON access.</p>
  {table}
</div></main>
"""
    canonical = f"{base_url}/s/"
    json_ld = blog_render._json_ld({
        "@context": "https://schema.org",
        "@type": "CollectionPage",
        "name": f"Stock snapshots | {SITE_NAME}",
        "url": canonical,
        "isPartOf": {"@type": "WebSite", "name": SITE_NAME, "url": base_url},
        "publisher": blog_render._organization_json_ld(base_url),
    })
    head = blog_render._head(
        f"Stock snapshots ({total} covered) | {SITE_NAME}",
        "Nightly-computed value, quality and momentum scores for every "
        f"stock {SITE_NAME} scans - one plain page per ticker.",
        canonical, base_url,
        extra_meta=f"<style>{_grid_css()}</style>", json_ld=json_ld,
    )
    return blog_render._page(head, body)
