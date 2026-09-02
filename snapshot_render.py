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
import score_history
import snapshot_store

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


def _valuation_note(pub):
    """`pub` is snapshot_store.public_view(data)'s output - see that
    function's own docstring for the internal->public field mapping
    (Fix 6, AI fixes round 2, 2026-08-31)."""
    val = pub.get("valuation_label")
    mos = pub.get("mos_pct")
    if val and val != "N/A" and mos is not None:
        return f"{val} (MOS {mos:+.1f}%)"
    return val or "-"


def _stat_cells(pub, moat):
    """[(label, value, hint), ...] for the stat grid. `pub` is
    snapshot_store.public_view(data)'s output, not the raw stored row -
    Fix 6, AI fixes round 2 (2026-08-31) dropped Signal/Trade Setup/
    Trend from every public surface (they read as recommendations on an
    indexable page) and renamed "Long Score" to the neutral "Value
    Score" the Deep Dive page's own factual view already uses. KPI
    order matches the round 2 instruction doc: Price, Intrinsic value,
    Margin of safety, Value Score, Quality, Moat as the primary row,
    Psychology/Discovery as secondary."""
    cells = [
        ("Price", _fmt(pub.get("price"), suffix=""), None),
        ("Intrinsic value", _fmt(pub.get("intrinsic_value")), None),
        ("Margin of safety",
         f"{pub['mos_pct']:+.1f}%" if pub.get("mos_pct") is not None else "-", None),
        ("Implied growth (reverse DCF)",
         f"{pub['implied_growth_pct']:+.1f}%" if pub.get("implied_growth_pct") is not None else "-",
         (f"model assumes {pub['model_growth_pct']:+.1f}%"
          if pub.get("model_growth_pct") is not None else "a described calculation from stated inputs, not a forecast")),
        ("Value Score", _fmt(pub.get("value_score")), None),
        ("Quality", _fmt(pub.get("quality")),
         "estimated (no reported figure)" if pub.get("quality_estimated") else None),
        ("Psychology", _fmt(pub.get("psychology")), None),
        ("Discovery", _fmt(pub.get("discovery")),
         "price/volume attention only"),
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


def _pct_phrase(p):
    """Local copy of app.py's _pct_phrase (Fix 2026-09-02, live: OCL.AX
    showed "top 0%" on the Deep Dive page) - clamps the displayed
    percentile to a minimum of 1 either way so a value very close to an
    extreme reads "top 1%"/"bottom 1%" rather than "top 0%", which reads
    as no standing at all. Duplicated rather than imported from app.py:
    this module is imported by the FastAPI routes app.py itself mounts,
    so importing app.py here would be circular - see compounder_ui.py's
    own docstring for this codebase's established convention of small,
    intentional duplication over a cross-module import in cases like
    this. Only changes the rounded DISPLAY text, never any stored or
    returned percentile value."""
    if p is None:
        return None
    if p >= 50:
        return f"top {max(1, round(100 - p))}%"
    return f"bottom {max(1, round(p))}%"


def _percentile_line(pub, universe):
    """Compact one-line percentile summary for under the KPI row (spec
    item 7, polish pass 2026-09-02): "Value Score: top 36% of S&P 500 -
    Quality: top 29%" - same neutral wording used elsewhere on the site.
    Percentiles come from pub["percentiles"] (snapshot_store.public_view's
    "Percentiles" -> "percentiles" mapping), itself written once per
    nightly scan by peer_context.attach_percentiles() - the SAME
    universe percentile the Deep Dive page's own Peer context block
    shows for this ticker, not a separate live recomputation, so the two
    can never disagree. Uses the "universe" percentile (not "sector") to
    match the spec's own example, which names the whole universe, not a
    sector. Value Score's phrase names the universe the page's own
    kicker line already shows; Quality's doesn't repeat it, matching the
    spec's example wording exactly. Returns None (renders nothing) when
    there's no percentile data at all yet - e.g. a snapshot saved before
    Percentiles started being attached, or a derived universe whose rows
    were copied from a parent scan (see nightly_scan.py/scheduler_engine.py's
    own comments on _DERIVED_UNIVERSE_PARENTS) - never a fabricated 0%."""
    pcts = pub.get("percentiles") or {}
    vs_phrase = _pct_phrase((pcts.get("value_score") or {}).get("universe"))
    q_phrase = _pct_phrase((pcts.get("quality") or {}).get("universe"))
    parts = []
    if vs_phrase:
        parts.append(f"Value Score: {vs_phrase}" + (f" of {universe}" if universe else ""))
    if q_phrase:
        parts.append(f"Quality: {q_phrase}")
    if not parts:
        return None
    return " &middot; ".join(parts)


def _dividend_line(pub):
    """Compact one-line dividend summary for under the percentile line
    (Services batch 3, Part A1): "Dividend: 1.2340/share (TTM) - yield
    3.1% - payout 42%" - the same "dividend_ttm"/"dividend_yield_pct"/
    "payout_ratio_pct" figures the Deep Dive's own Dividends panel shows
    (see app.py's _render_dividends_panel), reaching this page purely
    through the public whitelist (snapshot_store._PUBLIC_FIELD_MAP) - no
    separate computation here. None (renders nothing) for a ticker with
    no dividend history at all, same "omit rather than fabricate"
    convention _percentile_line above uses. Payout ratio is included only
    when present (a name with no EPS on file simply omits that clause,
    not "payout n/a")."""
    ttm = pub.get("dividend_ttm")
    if ttm is None:
        return None
    parts = [f"Dividend: {ttm:g}/share (TTM)"]
    if pub.get("dividend_yield_pct") is not None:
        parts.append(f"yield {pub['dividend_yield_pct']:.1f}%")
    if pub.get("payout_ratio_pct") is not None:
        parts.append(f"payout {pub['payout_ratio_pct']:.0f}%")
    return " &middot; ".join(parts)


def _score_history_line(ticker, today_score):
    """Server-rendered "Value Score 30 days ago: X -> today Y" text line
    (Services batch Part 5) - reads the same nightly-recorded history the
    Deep Dive page's own score-history chart uses (score_history.py);
    nothing computed here beyond formatting. None (renders nothing) when
    there's no stored history 30+ days back yet, or today's score isn't
    known - a missing history point should never be shown as "no
    change"."""
    if today_score is None:
        return None
    try:
        past = score_history.get(ticker, 30)
    except Exception:
        past = None
    if not past or past.get("long_score") is None:
        return None
    return f"Value Score 30 days ago: {past['long_score']:g} &rarr; today {today_score:g}"


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
.sdd-hist-line{color:#8aa0b8;font-size:14.5px;margin:-8px 0 18px}
.sdd-snap-pct{color:#8aa0b8;font-size:13.5px;margin:-20px 0 24px}
"""


def _snapshot_copy_text(ticker, pub, moat, generated, universe, lede, base_url,
                         company_name=None):
    """Fix 4, AI fixes round 1 (2026-08-31): the plain-text payload for
    this page's "Copy as text" button (blog_render._copy_as_text_html) -
    see that function's own docstring for the button/fallback mechanics.

    `pub` is snapshot_store.public_view(data)'s output, not the raw
    stored row - Fix 6, AI fixes round 2 (2026-08-31) dropped Signal/
    Trade Setup/Trend from this text (they read as recommendations) and
    renamed the fields to their public names; see _stat_cells()'s own
    comment for the authoritative field list this mirrors. This row
    shape is a strict subset of deep_dive_engine.analyze()'s full dict
    (no intrinsic-value method/source, no psychology/discovery
    sentiment labels), so this deliberately reads narrower than app.py's
    own _dd_copy_text() for the same reason _stat_cells() above only
    shows what's actually in `pub` - nothing invented here that isn't
    already on this page. `lede` is the same one-line summary string
    render_snapshot() already builds for the page itself (recomputing it
    here would risk it drifting from what's actually displayed)."""
    currency = "AUD" if ticker.upper().endswith(".AX") else "USD"
    lines = [f"{ticker} - {company_name}" if company_name and company_name != ticker else ticker]
    if generated:
        lines.append(f"Data generated {generated} UTC (nightly scan, universe: {universe or '-'}).")
    if pub.get("price") is not None:
        lines.append(f"Price: {_fmt(pub.get('price'))} {currency}")
    if pub.get("intrinsic_value") is not None:
        lines.append(f"Intrinsic value: {_fmt(pub.get('intrinsic_value'))} {currency}")
    if pub.get("mos_pct") is not None:
        lines.append(f"Margin of safety: {pub['mos_pct']:+.1f}%")
    if pub.get("implied_growth_pct") is not None:
        _igl = f"Implied growth (reverse DCF): {pub['implied_growth_pct']:+.1f}%"
        if pub.get("model_growth_pct") is not None:
            _igl += f" (model assumes {pub['model_growth_pct']:+.1f}%)"
        lines.append(_igl)
    if pub.get("valuation_label"):
        lines.append(f"Valuation: {pub['valuation_label']}")
    if pub.get("value_score") is not None:
        lines.append(f"Value Score: {_fmt(pub.get('value_score'))}")
    if pub.get("quality") is not None:
        lines.append(f"Quality: {_fmt(pub.get('quality'))}/100")
    if moat and moat.get("score") is not None:
        state = (moat.get("erosion") or "").replace("_", " ")
        lines.append(f"Moat score: {_fmt(moat.get('score'))}"
                     + (f" (erosion: {state})" if state else ""))
    if lede:
        lines.append(lede.replace("&middot;", "-").replace("&rarr;", "->"))

    flags = []
    if pub.get("quality_estimated"):
        flags.append("Quality rests on a default/estimated input (no reported figure).")
    if pub.get("intrinsic_estimated"):
        flags.append("Intrinsic value rests on a default/estimated input.")
    if flags:
        lines.append("Red flags: " + " ".join(flags))

    lines.append(f"Source: {SITE_NAME} - {snapshot_url(base_url, ticker)}")
    lines.append(PLAIN_DISCLAIMER)
    return "\n".join(lines)


def render_snapshot(snap, base_url):
    """snap is snapshot_store.get_snapshot(ticker)'s return value (not
    None - caller handles the unknown-ticker/404 case).

    Fix 6, AI fixes round 2 (2026-08-31): every number below now comes
    through snapshot_store.public_view(data) rather than the raw stored
    row - see that function's own docstring. This page no longer shows
    the LONG/AVOID-style Signal word anywhere (title, H1, lede or meta
    description) - those read as recommendations on an indexable page,
    which conflicts with the site's factual framing and its own
    disclaimer. "Long Score" is now "Value Score" throughout, matching
    the Deep Dive page's own factual-view wording."""
    e = html.escape
    ticker = snap["ticker"]
    data = snap.get("data") or {}
    pub = snapshot_store.public_view(data)
    moat = snap.get("moat")
    universe = snap.get("universe") or ""
    generated = (snap.get("generated_at") or "")[:16].replace("T", " ")
    company_name = pub.get("company_name")
    has_name = bool(company_name) and company_name != ticker

    cells = _stat_cells(pub, moat)
    cell_html = "".join(
        f'<div class="sdd-snap-cell"><div class="lbl">{e(label)}</div>'
        f'<div class="val">{e(str(value))}</div>'
        + (f'<div class="hint">{e(hint)}</div>' if hint else "")
        + "</div>"
        for label, value, hint in cells
    )

    valuation = _valuation_note(pub)
    lede = (f"{e(valuation)} &middot; Quality {e(_fmt(pub.get('quality')))}/100"
            if pub.get("valuation_label") else
            "Computed scores for this stock, from the same engine behind "
            "the site's Deep Dive and Scanner pages.")

    hist_line = _score_history_line(ticker, pub.get("value_score"))
    hist_html = f'<p class="sdd-hist-line">{hist_line}</p>' if hist_line else ""

    # Fix (2026-09-02, spec item 7): percentiles already reach this page's
    # own JSON payload but were never shown here - see _percentile_line()'s
    # own docstring.
    pct_line = _percentile_line(pub, universe)
    pct_html = f'<p class="sdd-snap-pct">{pct_line}</p>' if pct_line else ""

    # Services batch 3, Part A1: dividend headline numbers, same public
    # whitelist mechanism as the percentile line right above.
    div_line = _dividend_line(pub)
    div_html = f'<p class="sdd-snap-pct">{div_line}</p>' if div_line else ""

    display_name = f"{ticker} — {company_name}" if has_name else ticker
    title = (f"{display_name}: intrinsic value, Value Score, Moat | {SITE_NAME}"
              if has_name else f"{ticker} snapshot - computed scores | {SITE_NAME}")
    canonical = snapshot_url(base_url, ticker)
    cite_date = (snap.get("generated_at") or "")[:10]
    citation_text = f'{SITE_NAME}, "{ticker} snapshot,"' + (
        f" {cite_date}" if cite_date else "") + f". {canonical}"

    # Fix 4, AI fixes round 1 (2026-08-31): "Copy as text" - see
    # _snapshot_copy_text()'s and blog_render._copy_as_text_html()'s own
    # docstrings. dom_id is ticker-qualified since /s/<ticker> is a
    # distinct page per ticker but the id namespace is still global HTML.
    copy_text = _snapshot_copy_text(
        ticker, pub, moat, generated, universe, lede, base_url,
        company_name=company_name)
    copy_html = blog_render._copy_as_text_html(
        copy_text, dom_id=f"sdd-copytext-{ticker}")

    body = f"""
<main><div class="wrap">
  <div class="kicker">Snapshot &middot; {e(universe)} &middot; generated {e(generated)} UTC</div>
  <h1>{e(display_name)}</h1>
  <p class="lede">{lede}</p>
  {hist_html}
  {blog_render._copy_citation_html(citation_text)}
  {copy_html}
  <div class="sdd-snap-grid">{cell_html}</div>
  {pct_html}
  {div_html}
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
    desc_subject = f"{ticker} ({company_name})" if has_name else ticker
    facts = []
    if pub.get("intrinsic_value") is not None and pub.get("price") is not None:
        facts.append(
            f"intrinsic value {_fmt(pub['intrinsic_value'])} vs price {_fmt(pub['price'])}"
            + (f" (MOS {pub['mos_pct']:+.1f}%)" if pub.get("mos_pct") is not None else "")
        )
    if pub.get("value_score") is not None:
        facts.append(f"Value Score {_fmt(pub['value_score'])}")
    if pub.get("quality") is not None:
        facts.append(f"Quality {_fmt(pub['quality'])}")
    if moat and moat.get("score") is not None:
        facts.append(f"Moat {_fmt(moat['score'])}")
    if facts:
        description = (
            f"{desc_subject}: {', '.join(facts)} - computed scores from "
            f"{SITE_NAME}, descriptions of calculations, not recommendations."
        )
    else:
        description = (
            f"{desc_subject}: computed scores from {SITE_NAME}'s Deep Dive "
            "engine, updated nightly - descriptions of calculations, not "
            "recommendations."
        )
    json_ld = blog_render._json_ld({
        "@context": "https://schema.org",
        "@type": "WebPage",
        "name": title,
        "description": description,
        "url": canonical,
        "isPartOf": {"@type": "WebSite", "name": SITE_NAME, "url": base_url},
        "publisher": blog_render._organization_json_ld(base_url),
        "about": {"@type": "Corporation", "name": company_name or ticker},
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
