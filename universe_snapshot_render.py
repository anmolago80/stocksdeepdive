"""
universe_snapshot_render.py

SEO Commit C (18 Sep 2026, mocks/seo_snapshots_mock2.html, frame 1):
/s/universe/<slug> - one server-rendered page per overnight-scanned
universe (ASX 200, S&P 500, sector cuts, every real and derived
universe scan_store actually holds a scan for), EN + /es/. Same "reuse
blog_render's page shell" approach as snapshot_render.py (this module's
sibling, for the /s/<ticker> pages) - read-only, no write path, no user
data, every number already public on the live Scanner page.

Rendered LIVE from scan_store.load_scan() on every request (no extra
caching layer beyond the Cache-Control header - same 30-minute
convention /s/<ticker> already uses), so a page self-updates the moment
the next overnight scan lands, with nothing here to invalidate.

Universes are SELF-DISCOVERED from scan_store's own data directory
(_universe_slugs() below), never a hardcoded list - a universe added to
scheduler_engine's nightly cadence in the future gets a page the moment
its first scan completes, with zero code change here, and one dropped
from the cadence keeps its page (and its own stale-scan 404 once
load_scan()'s 72h cutoff passes) rather than needing a matching removal
somewhere.
"""

import glob
import html
import json
import os
from urllib.parse import quote as _urlquote

import blog_render
import scan_store
import snapshot_render
import snapshot_store

e = html.escape

TOP_N = 10

# Reused verbatim from i18n.py's home.top5.caption/home.top5.caption ES
# sibling (the home page's own "Tonight's top 5" section) - the exact
# established phrasing for "this is a sort, not a call", not a fresh
# paraphrase that could drift from it.
_SORT_NOT_RECOMMENDATION = {
    "en": "a sort result from described calculations, not a recommendation",
    "es": "un resultado de ordenamiento a partir de cálculos descritos, no una recomendación",
}


def _universe_slugs():
    """Every universe with a stored scan file on disk right now, sorted
    alphabetically - reads scan_store._data_dir() directly (the same
    "private"-by-convention cross-module read server.py already does
    for build_compounder_data._cp_data_dir()), never a hardcoded
    universe list."""
    try:
        paths = glob.glob(os.path.join(scan_store._data_dir(), "*.json"))
    except Exception:
        return []
    return sorted(os.path.splitext(os.path.basename(p))[0] for p in paths)


def universe_name_for_slug(slug):
    """The stored scan file's own "universe" display name for `slug`, or
    None if there's no file (unknown slug) or it's unreadable. Reads the
    file directly rather than keeping a separate slug<->name table, so
    it can never drift from what scan_store.save_scan() actually wrote -
    scan_store._slug(name) is a pure function, so re-deriving the slug
    from this name always reproduces the exact slug we started from."""
    try:
        path = os.path.join(scan_store._data_dir(), f"{slug}.json")
        with open(path, encoding="utf-8") as f:
            payload = json.load(f)
        return payload.get("universe")
    except Exception:
        return None


def _top_rows(rows):
    """Top TOP_N raw scan rows by Long Score (Value Score), highest
    first - rows with no score at all (a fetch failure that night) sort
    last rather than crashing on a None comparison, and are excluded
    once there are already TOP_N real ones."""
    scored = [r for r in rows if isinstance(r.get("Long Score"), (int, float))]
    scored.sort(key=lambda r: r["Long Score"], reverse=True)
    return scored[:TOP_N]


def _valuation_chip_html(valuation_label):
    if not valuation_label or valuation_label == "N/A":
        return ""
    cls = "mtu-lbl"
    low = valuation_label.lower()
    if "under" in low:
        cls += " mtu-lbl-g"
    elif "over" in low:
        cls += " mtu-lbl-o"
    return f'<span class="{cls}">{e(valuation_label.upper())}</span>'


def _table_html(top_rows, lang):
    if lang == "es":
        headers = ["#", "Ticker", "Empresa", "Value Score", "Valoración"]
    else:
        headers = ["#", "Ticker", "Company", "Value Score", "Valuation"]
    thead = "".join(f"<th>{e(h)}</th>" for h in headers)

    trs = []
    for i, row in enumerate(top_rows, start=1):
        pub = snapshot_store.public_view(row)
        ticker = row.get("Ticker") or ""
        # "Sector" is a raw scan-row field, not in snapshot_store's own
        # public whitelist (_PUBLIC_FIELD_MAP) - read directly here
        # rather than adding it to that whitelist, since this page is
        # the only caller that needs it and the whitelist's whole point
        # is that a new field is never exposed to every OTHER public
        # surface (API, /s/<ticker>) just because one new page wants it.
        # Sector itself is already public on the live Scanner page's own
        # sector filter (scanner_engine.py), so this is not new exposure
        # of anything private - just this one extra column.
        sector = row.get("Sector") or ""
        company = pub.get("company_name") or ticker
        value_score = pub.get("value_score")
        vs_str = f"{value_score:.1f}" if isinstance(value_score, (int, float)) else "–"
        company_cell = e(company)
        if sector:
            company_cell += f' <span class="mtu-sector">· {e(sector)}</span>'
        s_path = "/es/s" if lang == "es" else "/s"
        trs.append(
            f"<tr><td>{i}</td>"
            f'<td><a href="{s_path}/{e(ticker)}">{e(ticker)}</a></td>'
            f"<td>{company_cell}</td>"
            f"<td>{e(vs_str)}</td>"
            f"<td>{_valuation_chip_html(pub.get('valuation_label'))}</td></tr>"
        )
    return (f'<table class="sdd-snap-table mtu-table"><thead><tr>{thead}</tr></thead>'
            f'<tbody>{"".join(trs)}</tbody></table>')


_MTU_CSS = """
<style>
.mtu-table td{vertical-align:middle}
.mtu-sector{opacity:.6;font-size:12px}
.mtu-lbl{display:inline-block;border-radius:999px;padding:2px 10px;font-size:10.5px;
  font-weight:700;letter-spacing:.02em;border:1px solid currentColor;opacity:.9}
.mtu-lbl-g{color:#0f9d6b}
.mtu-lbl-o{color:#b45309}
.mtu-cta{display:inline-block;background:#14b8a6;color:#04211d;font-weight:800;
  font-size:13.5px;border-radius:10px;padding:10px 22px;margin:6px 0 22px;text-decoration:none}
.mtu-lede{margin:0 0 18px}
</style>
"""


def render_universe_snapshot(universe, payload, base_url, lang="en"):
    """`payload` is scan_store.load_scan(universe)'s return value (not
    None - caller handles the unknown/stale-scan 404 case, same pattern
    snapshot_page() already uses for an unknown ticker)."""
    slug = scan_store._slug(universe)
    en_path = f"/s/universe/{slug}"
    es_path = f"/es/s/universe/{slug}"
    path = es_path if lang == "es" else en_path
    canonical = f"{base_url}{path}"

    rows = payload.get("rows") or []
    top = _top_rows(rows)
    generated_label = payload.get("generated_at_label") or ""
    n_total = len(rows)

    scanner_href = f"/scanner?universe={_urlquote(universe)}"

    top_phrase_en = f"top {len(top)}" if len(top) != 1 else "one company"
    top_phrase_es = f"Los {len(top)} líderes" if len(top) != 1 else "La empresa líder"

    if lang == "es":
        h1 = f"{e(universe)} — escaneo nocturno de valor"
        lede = (
            f"Las {n_total} empresas escaneadas durante la noche"
            + (f" ({e(generated_label)})" if generated_label else "")
            + ": calidad del negocio, valor intrínseco frente al precio, "
              "psicología de la multitud y atención del mercado, combinados en un "
              f"solo Value Score. {top_phrase_es} en este momento — "
            + _SORT_NOT_RECOMMENDATION["es"] + "."
        )
        cta_label = f"Ver las {n_total} en el Buscador en vivo →" if n_total else "Abrir el Buscador →"
        kicker = "StocksDeepDive"
    else:
        h1 = f"{e(universe)} — overnight value scan"
        lede = (
            f"All {n_total} companies scanned overnight"
            + (f" ({e(generated_label)})" if generated_label else "")
            + ": business quality, intrinsic value vs price, crowd psychology and "
              "market attention, blended into one Value Score. "
              f"The {top_phrase_en} right now — "
            + _SORT_NOT_RECOMMENDATION["en"] + "."
        )
        cta_label = f"See all {n_total} in the live Scanner →" if n_total else "Open the Scanner →"
        kicker = "StocksDeepDive"

    if top:
        table = _table_html(top, lang)
    else:
        table = ('<div class="empty">No scored rows in this scan yet.</div>' if lang == "en"
                 else '<div class="empty">Todavía no hay filas puntuadas en este escaneo.</div>')

    body = f"""
<main><div class="wrap">
  <div class="kicker">{e(kicker)}</div>
  <h1>{e(h1)}</h1>
  <p class="lede mtu-lede">{lede}</p>
  {table}
  <a class="mtu-cta" href="{e(scanner_href)}">{e(cta_label)}</a>
</div></main>
"""
    description = (
        (f"{universe}: las {n_total} empresas del último escaneo nocturno, "
         "ordenadas por Value Score - cálculos descritos, no una recomendación.")
        if lang == "es" else
        (f"{universe}: all {n_total} companies from the latest overnight scan, "
         "ranked by Value Score - described calculations, not a recommendation.")
    )
    json_ld = blog_render._json_ld({
        "@context": "https://schema.org",
        "@type": "CollectionPage",
        "name": h1,
        "url": canonical,
        "isPartOf": {"@type": "WebSite", "name": blog_render.SITE_NAME, "url": base_url},
        "publisher": blog_render._organization_json_ld(base_url),
        "inLanguage": lang,
    })
    hreflang_alternates = [
        ("en", f"{base_url}{en_path}"),
        ("es", f"{base_url}{es_path}"),
        ("x-default", f"{base_url}{en_path}"),
    ]
    head = blog_render._head(
        f"{universe} — overnight value scan | {blog_render.SITE_NAME}" if lang == "en"
        else f"{universe} — escaneo nocturno de valor | {blog_render.SITE_NAME}",
        description, canonical, base_url,
        extra_meta=f"<style>{snapshot_render._grid_css()}</style>{_MTU_CSS}",
        json_ld=json_ld, hreflang_alternates=hreflang_alternates,
    )
    lang_urls = (f"{base_url}{en_path}", f"{base_url}{es_path}")
    return blog_render._page(head, body, lang=lang, path=path, lang_urls=lang_urls)
