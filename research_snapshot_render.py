"""
research_snapshot_render.py

SEO Commit D (18 Sep 2026, mocks/seo_snapshots_mock2.html, frame 2):
/s/research/<slug> - one server-rendered "article" page per hand-covered
Rational Compounder research company, EN + /es/. Same "reuse blog_render's
page shell" approach as snapshot_render.py/universe_snapshot_render.py -
read-only, no write path, renders EXACTLY the same content a real
signed-out visitor already sees on /research?ticker=<ticker> today -
never the News tab, never anything owner/subscriber-gated - so this page
can never show Google (or a person) anything the live app wouldn't show
an anonymous visitor.

DATA SOURCE: compounder_data.json (Andrew's own SMSF research workbook,
built by build_compounder_data.py), read the same way blog_render.py's
own _covered_tickers() already does - build_compounder_data._cp_data_dir()
resolves to the attached Railway Volume in production, this file's own
directory otherwise. This module never writes to that file. Deliberately
does NOT import app.py (a Streamlit entrypoint, unsafe to import outside
a running Streamlit session) or compounder_ui.py (imports streamlit +
plotly - real weight for a FastAPI process to carry for two things this
module needs) - instead duplicates the two tiny PURE-PYTHON pieces of
logic it actually needs from each:
  - _fmt(): the exact same value formatting as compounder_ui._cp_format()
    (pct/x/cur/num) - kept in sync with that function by hand; if it ever
    changes there, mirror the change here.
  - _PAYWALL_ENABLED: the exact same PAYWALL_ENABLED truthy-env-var check
    paywall_engine.py itself computes at import time (see that module's
    own docstring for what "enabled" means) - reading the same
    PAYWALL_ENABLED env var directly rather than importing paywall_engine
    (which imports streamlit). A snapshot-page visitor is never signed in
    (there is no session at all), so the real gating condition paywall_
    engine.render_gate() would apply to them collapses to exactly this
    one flag - is_subscribed(None) is always False regardless of Stripe
    config (see paywall_engine.is_subscribed()'s own fail-closed-on-no-
    email behaviour), so there is nothing else to replicate.

WHAT RENDERS, WHAT DOESN'T (mirrors app.py's _render_research_detail /
_render_cp_section exactly):
  - Fundamentals, Value vs Book, Retained Earnings, Earnings Trends, Cost
    of Capital: always public (paywall_engine.py's own docstring: "Home,
    Stock Scanner, ... Research's other sections stays free regardless of
    this module") - metric label + formatted value only, no charts (zero-
    JS constraint anyway rules out Plotly - the CTA links to the live
    interactive page for the charts), no colour thresholds/bands (a
    deliberate, conservative extra step beyond what render_section()
    itself withholds - Andrew's own colour-band THRESHOLDS are a closer
    cousin to "the recipe" than the plain numbers are, so they're left
    out here even though the interactive page does show them), no
    per-metric `comment` text either (his own annotation can itself spell
    out the interpretation bands in prose - see build_compounder_data.py's
    own module docstring - so it carries the same risk as the numeric
    thresholds and is held to the same conservative bar).
  - Fair Value, Company Potential: rendered ONLY when not gated (see
    _PAYWALL_ENABLED above) - exactly mirroring what a real signed-out
    visitor sees today. When PAYWALL_ENABLED flips on in production, this
    page starts omitting them automatically, with zero code change here.
  - Company Potential's Low/Medium/High ratings and short Yes/No/Medium
    checks: shown as plain "label: value" text - no colour pills (same
    conservative reasoning as the metric thresholds above).
  - Company Potential's written text_groups: shown VERBATIM ("display,
    don't rewrite" - the same rule app.py's own _rc_verdict_text()
    docstring states) with exactly ONE deliberate exception: the specific
    "Investment Recommendation" item inside "The Investment Case" group -
    app.py's own docstring calls this "the closing buy/hold/pass call" -
    is never rendered on this page. This is a judgment call, not
    something the task spec explicitly asked for: it's the one piece of
    this hand-written research that reads as investment ADVICE rather
    than description, and this whole SEO rollout's standing rule (no
    advice wording, on a page Google indexes) applies with extra force
    here. Every other item in every other text group - including "Why Is
    This a Good Investment?", which is thesis reasoning rather than a
    closing call - renders unchanged. The long-form Yes/No answers
    app.py's own _render_cp_section() merges into "The Investment Case"
    at render time (any check answer over 30 characters) are reproduced
    here with the identical merge, so this page's Investment Case
    section matches the interactive page's, item-for-item, minus that
    one excluded label.

  News tab: never reaches this module at all - it isn't workbook data
  (compounder_ui.render_news_tab() fetches live), so there's nothing to
  read from compounder_data.json for it, and no code path here could
  render it even by accident.

SLUG: "<ticker-part>-<company-slug>", e.g. "OCL.AX" / "Objective
Corporation" -> "ocl-objective-corporation" (matches the approved mock's
own example URL) - ticker-part is the ticker with its exchange suffix
dropped and lowercased (".AX" stripped, not just lowercased, so US
tickers with no suffix and ASX tickers with one produce equally clean
slugs), company-slug from snapshot_store's own cached company name
(never a live lookup - see _company_name() below) when available, omitted
when it isn't (a ticker not yet through a nightly scan still gets a
ticker-only slug rather than no page at all). Slugs are SELF-DISCOVERED
from compounder_data.json's own ticker list (never a hardcoded list),
filtered to tickers that actually have renderable public content (see
_research_tickers() below) - a company Andrew adds real coverage for
gets a page the moment the next compounder_data.json rebuild includes it,
with zero code change here.
"""

import html
import os
import re

import blog_render
import snapshot_store

e = html.escape

SITE_NAME = blog_render.SITE_NAME

# Kept in sync with paywall_engine.PAYWALL_ENABLED's own definition (see
# module docstring above for why this module reads the env var directly
# rather than importing paywall_engine).
_PAYWALL_ENABLED = (os.environ.get("PAYWALL_ENABLED") or "").strip().lower() in (
    "1", "true", "yes", "on",
)

# Mirrors app.py's own section_order filtered to "sections in data" - the
# six computed sections this page can show (Company Potential is handled
# separately, exactly as app.py's own section_order construction does).
_SECTION_ORDER = [
    "Fundamentals", "Value vs Book", "Retained Earnings",
    "Earnings Trends", "Cost of Capital", "Fair Value",
]
_GATED_SECTIONS = {"Fair Value", "Company Potential"}

_SECTION_LABEL_ES = {
    "Fundamentals": "Fundamentos",
    "Value vs Book": "Valor vs Libros",
    "Retained Earnings": "Utilidades Retenidas",
    "Earnings Trends": "Tendencias de Ganancias",
    "Cost of Capital": "Costo de Capital",
    "Fair Value": "Valor Razonable",
}

# The one item this page deliberately never renders - see module
# docstring's "WHAT RENDERS, WHAT DOESN'T" section.
_EXCLUDED_TEXT_ITEM_LABELS = {"Investment Recommendation"}
_CHECK_LONGFORM_MIN = 30  # identical threshold to app.py's own _render_cp_section


def _load_research_data():
    """compounder_data.json, or None if it doesn't exist yet (no volume,
    or a volume that's never been rebuilt) - same read pattern as blog_
    render._covered_tickers(), one level up (returns the whole dict
    rather than just the ticker set, since this module needs the
    sections/metrics too)."""
    try:
        import build_compounder_data
        import json
        path = os.path.join(build_compounder_data._cp_data_dir(), "compounder_data.json")
        if not os.path.exists(path):
            return None
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _ticker_slug_part(ticker):
    return re.sub(r"[^a-z0-9]+", "", (ticker or "").split(".")[0].lower())


def _company_slug_part(name):
    if not name:
        return ""
    return re.sub(r"-{2,}", "-", re.sub(r"[^a-z0-9]+", "-", name.lower())).strip("-")


def _company_name(ticker):
    """Same read-only snapshot_store lookup as app.py's own
    _rc_company_name() - the nightly scan's cached name, never a live
    fetch. None when this ticker hasn't been through a nightly scan yet
    (some hand-covered names may not be in any scanned universe)."""
    try:
        snap = snapshot_store.get_snapshot(ticker)
        if not snap:
            return None
        return snapshot_store.public_view(snap.get("data") or {}).get("company_name")
    except Exception:
        return None


def slug_for_ticker(ticker, company_name=None):
    tpart = _ticker_slug_part(ticker)
    cpart = _company_slug_part(company_name if company_name is not None else _company_name(ticker))
    return f"{tpart}-{cpart}" if cpart else tpart


def _has_public_content(ticker, data):
    """True when this ticker has at least one of: a computed-section
    metric with a real value, or Company Potential ratings/checks/groups
    - i.e. there is SOMETHING this page could show, gated or not (a
    ticker whose only coverage is a still-gated Fair Value/Company
    Potential entry still gets a page today - it'll show the five free
    sections plus header/CTA, same as the interactive page would for
    that same signed-out visitor)."""
    sections = data.get("sections") or {}
    for label in _SECTION_ORDER:
        metrics = (sections.get(label) or {}).get("metrics") or []
        if any((m.get("values") or {}).get(ticker) is not None for m in metrics):
            return True
    cp = sections.get("Company Potential") or {}
    if (cp.get("hml_ratings") or {}).get(ticker):
        return True
    if (cp.get("yesno_checks") or {}).get(ticker):
        return True
    if (cp.get("text_groups") or {}).get(ticker):
        return True
    return False


def _research_tickers(data):
    """Every ticker with real public-content (see _has_public_content),
    sorted - the self-discovered page set, mirrored 1:1 by the sitemap
    loop in server.py."""
    tickers = (data or {}).get("tickers") or {}
    return sorted(t for t in tickers if _has_public_content(t, data))


def slug_map(data):
    """{slug: ticker} for every research ticker right now - built fresh
    each call (compounder_data.json is loaded once per request anyway;
    this is a handful of hand-covered companies, not thousands)."""
    out = {}
    for t in _research_tickers(data):
        out[slug_for_ticker(t)] = t
    return out


def ticker_for_slug(slug, data):
    return slug_map(data).get(slug)


def _fmt(value, fmt):
    """Identical to compounder_ui._cp_format() - see module docstring for
    why this is duplicated rather than imported."""
    if value is None:
        return None
    if fmt == "text":
        return str(value) if str(value).strip() else None
    if fmt == "pct":
        return f"{value * 100:,.1f}%"
    if fmt == "x":
        return f"{value:,.2f}x"
    if fmt == "cur":
        return f"${value:,.0f}" if abs(value) >= 1000 else f"${value:,.2f}"
    return f"{value:,.2f}"


def _section_html(section_label, section, ticker, lang):
    metrics = section.get("metrics") or []
    rows = []
    for m in metrics:
        val = (m.get("values") or {}).get(ticker)
        if val is None:
            continue
        shown = _fmt(val, m.get("format") or "num")
        if shown is None:
            continue
        rows.append(f"<tr><td>{e(m.get('label') or m.get('key') or '')}</td><td>{e(shown)}</td></tr>")
    if not rows:
        return ""
    heading = _SECTION_LABEL_ES[section_label] if lang == "es" and section_label in _SECTION_LABEL_ES else section_label
    anchor = _ticker_slug_part(section_label) or section_label.lower().replace(" ", "-")
    anchor = re.sub(r"[^a-z0-9]+", "-", section_label.lower()).strip("-")
    return (
        f'<h2 id="{anchor}">{e(heading)}</h2>'
        f'<table class="rsn-table"><tbody>{"".join(rows)}</tbody></table>'
    )


def _company_potential_html(cp_section, ticker, lang):
    """Ratings + short checks (plain label:value list) then the written
    text_groups (verbatim, minus the one excluded item) - see module
    docstring. Returns "" when there's nothing to show for this ticker."""
    ratings = list((cp_section.get("hml_ratings") or {}).get(ticker) or [])
    checks = list((cp_section.get("yesno_checks") or {}).get(ticker) or [])
    groups = [dict(g, items=list(g.get("items") or [])) for g in
              ((cp_section.get("text_groups") or {}).get(ticker) or [])]

    short_checks = [c for c in checks if len((c.get("value") or "").strip()) <= _CHECK_LONGFORM_MIN]
    long_checks = [c for c in checks if len((c.get("value") or "").strip()) > _CHECK_LONGFORM_MIN]

    if long_checks:
        ic_items = [{"label": c.get("label"), "text": c.get("value")} for c in long_checks]
        ic_group = next((g for g in groups if g.get("title") == "The Investment Case"), None)
        if ic_group is not None:
            ic_group["items"] = ic_group["items"] + ic_items
        else:
            groups = groups + [{"title": "The Investment Case", "items": ic_items}]

    if not ratings and not short_checks and not groups:
        return ""

    parts = []
    heading = "Notas de investigación del autor" if lang == "es" else "Author's research notes"
    parts.append(f'<h2 id="research-notes">{e(heading)}</h2>')

    rating_rows = []
    for r in ratings:
        label = (r.get("label") or "").strip()
        value = (r.get("value") or "").strip()
        if label and value:
            rating_rows.append(f"<tr><td>{e(label)}</td><td>{e(value)}</td></tr>")
    for c in short_checks:
        label = (c.get("label") or "").strip()
        value = (c.get("value") or "").strip()
        if label and value:
            rating_rows.append(f"<tr><td>{e(label)}</td><td>{e(value)}</td></tr>")
    if rating_rows:
        parts.append(f'<table class="rsn-table">{"".join(rating_rows)}</table>')

    for g in groups:
        title = (g.get("title") or "").strip()
        items = [
            it for it in (g.get("items") or [])
            if (it.get("label") or "").strip() not in _EXCLUDED_TEXT_ITEM_LABELS
            and (it.get("text") or "").strip()
        ]
        if not items:
            continue
        if title:
            parts.append(f"<h3>{e(title)}</h3>")
        for it in items:
            label = (it.get("label") or "").strip()
            text = (it.get("text") or "").strip()
            body = e(text).replace("\n", "<br>")
            if label:
                parts.append(f"<p class='rsn-item'><b>{e(label)}:</b> {body}</p>")
            else:
                parts.append(f"<p class='rsn-item'>{body}</p>")

    return "".join(parts)


_RSN_CSS = """
<style>
.rsn-table{width:100%;border-collapse:collapse;font-size:13.5px;margin:6px 0 18px}
.rsn-table td{padding:5px 10px;border-bottom:1px solid rgba(255,255,255,.06)}
.rsn-table td:first-child{color:#8aa0b8}
.rsn-table td:last-child{text-align:right;font-variant-numeric:tabular-nums}
.rsn-item{margin:0 0 12px;line-height:1.7}
.rsn-toc{margin:8px 0 22px;font-size:13px;color:#8aa0b8}
.rsn-toc a{color:#2dd4bf;text-decoration:none}
.rsn-cta{display:inline-block;background:#14b8a6;color:#04211d;font-weight:800;
  font-size:13.5px;border-radius:10px;padding:10px 22px;margin:10px 0 22px;text-decoration:none}
.rsn-flag{color:#fbbf24;font-size:12px;margin:0 0 14px}
</style>
"""


def render_research_snapshot(ticker, data, base_url, lang="en"):
    """`data` is _load_research_data()'s return value (not None - caller
    handles the not-ready-yet case, same pattern every other snapshot
    route in this codebase uses)."""
    ticker = (ticker or "").strip().upper()
    company_name = _company_name(ticker)
    slug = slug_for_ticker(ticker, company_name)
    en_path = f"/s/research/{slug}"
    es_path = f"/es/s/research/{slug}"
    path = es_path if lang == "es" else en_path
    canonical = f"{base_url}{path}"

    sections = data.get("sections") or {}
    generated_at = (data.get("generated_at") or "")[:10]

    display_name = f"{company_name} ({ticker})" if company_name else ticker
    if lang == "es":
        h1 = f"\U0001F4DA {display_name} — investigación Rational Compounder"
        lead = (
            "Investigación hecha a mano: fundamentos, valor vs libros, prueba de "
            "utilidades retenidas, tendencias de ganancias, costo de capital y valor "
            "razonable — publicada tal como fue escrita"
            + (f", actualizada por última vez el {e(generated_at)}" if generated_at else "")
            + "."
        )
        flag = "Investigación original en inglés — mostrada tal como fue escrita."
        cta_label = "Abrir la vista interactiva de Investigación →"
        toc_label = "Contenido:"
        footer = (
            "Presentación pública únicamente — la misma vista que ve un "
            "visitante sin sesión iniciada en la app; la pestaña de Noticias y "
            "cualquier contenido restringido nunca se muestra aquí. Investigación, "
            "no un consejo."
        )
    else:
        h1 = f"\U0001F4DA {display_name} — Rational Compounder research"
        lead = (
            "Hand-covered research: fundamentals, value vs book, retained-earnings "
            "test, earnings trends, cost of capital and fair value — published as "
            "written"
            + (f", last updated {e(generated_at)}" if generated_at else "")
            + "."
        )
        flag = None
        cta_label = "Open the interactive Research view →"
        toc_label = "Contents:"
        footer = (
            "Public factual presentation only — the same view a signed-out "
            "visitor gets in the app; the News tab and anything owner/subscriber-"
            "gated never renders here. Research, not advice."
        )

    section_blocks = []
    toc_entries = []
    for label in _SECTION_ORDER:
        if label in _GATED_SECTIONS and _PAYWALL_ENABLED:
            continue
        section = sections.get(label)
        if not section:
            continue
        block = _section_html(label, section, ticker, lang)
        if block:
            section_blocks.append(block)
            anchor = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")
            heading = _SECTION_LABEL_ES[label] if lang == "es" and label in _SECTION_LABEL_ES else label
            toc_entries.append(f'<a href="#{anchor}">{e(heading)}</a>')

    cp_html = ""
    if not _PAYWALL_ENABLED:
        cp_section = sections.get("Company Potential") or {}
        cp_html = _company_potential_html(cp_section, ticker, lang)

    toc_html = f'<div class="rsn-toc">{e(toc_label)} {" · ".join(toc_entries)}</div>' if toc_entries else ""
    flag_html = f'<p class="rsn-flag">{e(flag)}</p>' if flag else ""

    cta_url = f"/research?ticker={ticker}" + ("&lang=es" if lang == "es" else "")

    body = f"""
<main><div class="wrap">
  <div class="kicker">{e(SITE_NAME)}</div>
  <h1>{h1}</h1>
  <p class="lede">{lead}</p>
  {toc_html}
  {flag_html}
  {cp_html}
  {''.join(section_blocks)}
  <a class="rsn-cta" href="{e(cta_url)}">{e(cta_label)}</a>
  <p class="cap">{e(footer)}</p>
</div></main>
"""
    description = (
        (f"{company_name or ticker}: investigación Rational Compounder hecha a mano — "
         "fundamentos, valor vs libros, tendencias de ganancias y más, publicada tal como "
         "fue escrita.")
        if lang == "es" else
        (f"{company_name or ticker}: hand-covered Rational Compounder research — "
         "fundamentals, value vs book, earnings trends and more, published as written.")
    )
    json_ld = blog_render._json_ld({
        "@context": "https://schema.org",
        "@type": "Article",
        "headline": h1,
        "url": canonical,
        "about": {"@type": "Corporation", "name": company_name or ticker, "tickerSymbol": ticker},
        "isPartOf": {"@type": "WebSite", "name": SITE_NAME, "url": base_url},
        "author": blog_render._person_json_ld(),
        "publisher": blog_render._organization_json_ld(base_url),
        "inLanguage": lang,
        **({"dateModified": data.get("generated_at")} if data.get("generated_at") else {}),
    })
    hreflang_alternates = [
        ("en", f"{base_url}{en_path}"),
        ("es", f"{base_url}{es_path}"),
        ("x-default", f"{base_url}{en_path}"),
    ]
    head = blog_render._head(
        f"{h1} | {SITE_NAME}", description, canonical, base_url,
        extra_meta=_RSN_CSS, json_ld=json_ld, hreflang_alternates=hreflang_alternates,
    )
    lang_urls = (f"{base_url}{en_path}", f"{base_url}{es_path}")
    return blog_render._page(head, body, lang=lang, path=path, lang_urls=lang_urls)
