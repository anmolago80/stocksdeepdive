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
from datetime import datetime

import blog_render
import i18n
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


def _valuation_note(pub, lang="en"):
    """`pub` is snapshot_store.public_view(data)'s output - see that
    function's own docstring for the internal->public field mapping
    (Fix 6, AI fixes round 2, 2026-08-31). `pub["valuation_label"]`
    itself is an engine-computed status WORD (e.g. "Undervalued") and
    stays untranslated either way, per this codebase's standing rule for
    engine output (Español completion, Part 1's own rule, reused here).
    "MOS" itself stays "MOS" in Spanish too - same established choice as
    i18n.py's own dd.legend.mos/alert.metric.mos_pct ES entries, both of
    which keep the bare abbreviation rather than inventing a Spanish one;
    `lang` therefore has no effect on this particular string, kept as a
    parameter only so every caller in this module has one uniform
    signature."""
    val = pub.get("valuation_label")
    mos = pub.get("mos_pct")
    if val and val != "N/A" and mos is not None:
        return f"{val} (MOS {mos:+.1f}%)"
    return val or "-"


def _not_trading_banner(ticker, lang="en"):
    """Commit J (21 Sep 2026, owner-reported): the "not currently
    trading" banner for a ticker nightly_scan.analyze_ticker_lite()'s
    ghost-price guard flagged "stale" - a delisted/halted/merged
    company yfinance keeps quoting the last real print for, forever
    (QUB.AX: an exact 5.11 close on every day from 2026-08-20 to
    2026-09-17; LSF.AX: unchanged across its last several scans too -
    both found live via a 2026-09-20 DB backup, LSF still ranking #12
    in the ASX 200 scanner table with mos_pct 95.2 before this fix).

    The last REAL trading date comes from score_history.
    last_real_price_date(ticker) - the day the series' own price last
    genuinely changed, not the most recent day this ticker happened to
    be scanned (which, for a ghost-priced ticker, is every night,
    always repeating the same frozen number) - see that function's own
    docstring. None if there's no history to derive it from yet (a
    brand-new flag with no accumulated series) - the no-date fallback
    copy is used instead, never a fabricated date."""
    day = score_history.last_real_price_date(ticker)
    date_label = None
    if day:
        try:
            date_label = datetime.strptime(day, "%Y-%m-%d").strftime("%d %b %Y")
        except ValueError:
            date_label = None
    if date_label:
        return i18n.t("snapshot.not_trading_banner", lang, date=date_label)
    return i18n.t("snapshot.not_trading_banner_no_date", lang)


def _stat_cells(pub, moat, lang="en"):
    """[(label, value, hint), ...] for the stat grid. `pub` is
    snapshot_store.public_view(data)'s output, not the raw stored row -
    Fix 6, AI fixes round 2 (2026-08-31) dropped Signal/Trade Setup/
    Trend from every public surface (they read as recommendations on an
    indexable page) and renamed "Long Score" to the neutral "Value
    Score" the Deep Dive page's own factual view already uses. KPI
    order matches the round 2 instruction doc: Price, Intrinsic value,
    Margin of safety, Value Score, Quality, Moat as the primary row,
    Psychology/Discovery as secondary.

    lang (Español completion, Part 3): only the LABEL and hint text
    translate - the established glossary terms (Puntaje Value/Calidad/
    Psicología/Descubrimiento/Foso, Margen de Seguridad) - the numeric
    VALUE column is untouched data either way."""
    if lang == "es":
        cells = [
            ("Precio", _fmt(pub.get("price"), suffix=""), None),
            ("Valor intrínseco", _fmt(pub.get("intrinsic_value")), None),
            ("Margen de seguridad",
             f"{pub['mos_pct']:+.1f}%" if pub.get("mos_pct") is not None else "-", None),
            ("Crecimiento implícito (DCF inverso)",
             f"{pub['implied_growth_pct']:+.1f}%" if pub.get("implied_growth_pct") is not None else "-",
             (f"el modelo asume {pub['model_growth_pct']:+.1f}%"
              if pub.get("model_growth_pct") is not None else "un cálculo descrito a partir de datos declarados, no un pronóstico")),
            ("Puntaje Value", _fmt(pub.get("value_score")), None),
            ("Calidad", _fmt(pub.get("quality")),
             "estimado (sin cifra reportada)" if pub.get("quality_estimated") else None),
            ("Psicología", _fmt(pub.get("psychology")), None),
            ("Descubrimiento", _fmt(pub.get("discovery")),
             "solo atención de precio/volumen"),
        ]
        if moat:
            moat_score = moat.get("score")
            erosion = (moat.get("erosion") or "").replace("_", " ")
            cells.append((
                "Puntaje de foso",
                _fmt(moat_score) if moat_score is not None else "n/d",
                f"erosión: {erosion}" if erosion else None,
            ))
        return cells
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


def _pct_phrase(p, lang="en"):
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
    returned percentile value.

    lang (Español completion, Part 3): same "top X%"/"bottom X%" ->
    "entre el X% mejor"/"entre el X% más bajo" phrasing app.py's own
    _pct_phrase already uses for lang="es" (dd.peer.pct_top/pct_bottom)."""
    if p is None:
        return None
    if lang == "es":
        if p >= 50:
            return f"entre el {max(1, round(100 - p))}% mejor"
        return f"entre el {max(1, round(p))}% más bajo"
    if p >= 50:
        return f"top {max(1, round(100 - p))}%"
    return f"bottom {max(1, round(p))}%"


def _percentile_line(pub, universe, lang="en"):
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
    own comments on _DERIVED_UNIVERSE_PARENTS) - never a fabricated 0%.

    lang (Español completion, Part 3): "Value Score"/"Quality" labels
    translate to the established glossary terms; universe names (e.g.
    "S&P 500") stay as-is - they're data, not prose."""
    pcts = pub.get("percentiles") or {}
    vs_phrase = _pct_phrase((pcts.get("value_score") or {}).get("universe"), lang)
    q_phrase = _pct_phrase((pcts.get("quality") or {}).get("universe"), lang)
    parts = []
    vs_label = "Puntaje Value" if lang == "es" else "Value Score"
    q_label = "Calidad" if lang == "es" else "Quality"
    of_word = "de" if lang == "es" else "of"
    if vs_phrase:
        parts.append(f"{vs_label}: {vs_phrase}" + (f" {of_word} {universe}" if universe else ""))
    if q_phrase:
        parts.append(f"{q_label}: {q_phrase}")
    if not parts:
        return None
    return " &middot; ".join(parts)


def _dividend_line(pub, lang="en"):
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
    not "payout n/a").

    lang (Español completion, Part 3): "Dividend"/"yield"/"payout" words
    translate; the numbers themselves don't."""
    ttm = pub.get("dividend_ttm")
    if ttm is None:
        return None
    if lang == "es":
        parts = [f"Dividendo: {ttm:g}/acción (TTM)"]
        if pub.get("dividend_yield_pct") is not None:
            parts.append(f"rendimiento {pub['dividend_yield_pct']:.1f}%")
        if pub.get("payout_ratio_pct") is not None:
            parts.append(f"pago {pub['payout_ratio_pct']:.0f}%")
        return " &middot; ".join(parts)
    parts = [f"Dividend: {ttm:g}/share (TTM)"]
    if pub.get("dividend_yield_pct") is not None:
        parts.append(f"yield {pub['dividend_yield_pct']:.1f}%")
    if pub.get("payout_ratio_pct") is not None:
        parts.append(f"payout {pub['payout_ratio_pct']:.0f}%")
    return " &middot; ".join(parts)


def _faq_items(ticker, data, pub, company_name, cite_date, lang="en"):
    """SEO Commit E (18 Sep 2026, mocks/snapshot_faq_schema_mock.html):
    the "Common questions" FAQ entries for this ticker's snapshot page -
    every fact pulled from `pub`/`data` (the SAME stored scan row
    render_snapshot() already has in memory), never a second fetch, so an
    answer can never disagree with the numbers shown above it on the same
    page. Returns [{"question", "answer"}, ...] in a fixed, spec'd order,
    each one included only when its underlying data actually exists (see
    each block's own comment) - the standing "omit rather than fabricate"
    convention every other optional line on this page already follows.
    The trailing not-a-recommendation disclaimer is NOT included here -
    render_snapshot() appends it to whichever item ends up first in the
    returned list, so it's always present exactly once regardless of
    which questions happen to have data for a given ticker."""
    display_name = company_name or ticker
    items = []

    # (1) "Is <TICKER> undervalued?" - needs price, intrinsic value, MOS%
    # and a real (non-"N/A") valuation label all at once, since the
    # answer sentence references all four together. Commit J: never asked
    # at all for a ticker flagged "stale" - a frozen ghost price can no
    # longer honestly support an undervalued/expensive claim, same
    # reasoning as render_snapshot()'s own lede override.
    price, intrinsic, mos, valn = (
        pub.get("price"), pub.get("intrinsic_value"), pub.get("mos_pct"), pub.get("valuation_label"),
    )
    if (price is not None and intrinsic is not None and mos is not None and valn and valn != "N/A"
            and pub.get("trading_status") != "stale"):
        items.append({
            "question": i18n.t("snapshot.faq.undervalued_q", lang, ticker=ticker),
            # valn is an engine-output status word (e.g. "Undervalued") -
            # passed through unchanged, never translated, same standing
            # rule _valuation_note() above already documents.
            "answer": i18n.t(
                "snapshot.faq.undervalued_a", lang,
                ticker=ticker, date=cite_date, price=_fmt(price), intrinsic=_fmt(intrinsic),
                mos=f"{mos:+.1f}%", valuation=valn,
            ),
        })

    # (2) "What is <Company>'s Value Score?" - needs the score itself.
    value_score = pub.get("value_score")
    if value_score is not None:
        items.append({
            "question": i18n.t("snapshot.faq.value_score_q", lang, company=display_name),
            "answer": i18n.t(
                "snapshot.faq.value_score_a", lang,
                company=display_name, score=_fmt(value_score), date=cite_date,
            ),
        })

    # (3) "Does <TICKER> pay a dividend?" - needs the TTM figure; yield is
    # included in the sentence only when it's also on file (same
    # "never omit the whole line for a missing yield" convention
    # _dividend_line() above already uses).
    ttm = pub.get("dividend_ttm")
    if ttm is not None:
        yield_pct = pub.get("dividend_yield_pct")
        key = "snapshot.faq.dividend_a_with_yield" if yield_pct is not None else "snapshot.faq.dividend_a_no_yield"
        kwargs = {"ticker": ticker, "ttm": f"{ttm:g}", "date": cite_date}
        if yield_pct is not None:
            kwargs["yield_pct"] = f"{yield_pct:.1f}"
        items.append({
            "question": i18n.t("snapshot.faq.dividend_q", lang, ticker=ticker),
            "answer": i18n.t(key, lang, **kwargs),
        })

    # (4) "What does <Company> do?" - first sentence of the provider
    # profile, WITH the existing provenance convention (dd.about.
    # provenance's own established wording, reused verbatim here). Reads
    # from a raw "Business Summary" field on the stored row - nightly_
    # scan.py has never populated one (confirmed before writing this;
    # out of scope for this commit, whose diff is confined to snapshot_
    # render.py + i18n.py), so this item does not render on any ticker
    # today. Kept as real, working plumbing rather than left out
    # entirely, so a FUTURE, separately-sanctioned change that starts
    # caching a profile sentence into the nightly scan row makes this
    # question start appearing with zero further change here - and so
    # this function fully covers the task spec's own four-question set
    # rather than silently dropping the one with no data source yet.
    summary_raw = (data.get("Business Summary") or "").strip()
    if summary_raw:
        first = re.split(r"(?<=[.!?])\s+", summary_raw, maxsplit=1)[0].strip()
        items.append({
            "question": i18n.t("snapshot.faq.about_q", lang, company=display_name),
            "answer": i18n.t(
                "snapshot.faq.about_a", lang,
                sentence=first, provenance=i18n.t("snapshot.faq.about_provenance", lang),
            ),
        })

    return items



def _score_history_line(ticker, today_score, lang="en"):
    """Server-rendered "Value Score 30 days ago: X -> today Y" text line
    (Services batch Part 5) - reads the same nightly-recorded history the
    Deep Dive page's own score-history chart uses (score_history.py);
    nothing computed here beyond formatting. None (renders nothing) when
    there's no stored history 30+ days back yet, or today's score isn't
    known - a missing history point should never be shown as "no
    change".

    lang (Español completion, Part 3): the sentence around the numbers
    translates; the numbers themselves don't."""
    if today_score is None:
        return None
    try:
        past = score_history.get(ticker, 30)
    except Exception:
        past = None
    if not past or past.get("long_score") is None:
        return None
    if lang == "es":
        return f"Puntaje Value hace 30 días: {past['long_score']:g} &rarr; hoy {today_score:g}"
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
.lede.sdd-snap-stale{color:#fbbf24;font-weight:600;font-size:16.5px}
.sdd-snap-pct{color:#8aa0b8;font-size:13.5px;margin:-20px 0 24px}
.sdd-faq{margin:30px 0 10px}
.sdd-faq h2{font-size:17px;margin:0 0 12px}
.sdd-faq-item{background:#121f36;border:1px solid #1f3352;border-radius:10px;
  padding:12px 16px;margin-bottom:10px}
.sdd-faq-item summary{cursor:pointer;font-weight:600;font-size:14.5px;color:#e6edf5;
  list-style:none}
.sdd-faq-item summary::-webkit-details-marker{display:none}
.sdd-faq-item summary::before{content:"+ ";color:#2dd4bf;font-weight:800}
.sdd-faq-item[open] summary::before{content:"\\2212 "}
.sdd-faq-item p{color:#c7d2e0;font-size:13.5px;line-height:1.65;margin:8px 0 0}
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
    # Commit J: suppress the valuation label for a ticker flagged
    # "stale" - same reasoning as render_snapshot()'s own lede
    # override right above this function's call site; `lede` itself
    # already carries the "not currently trading" banner text in that
    # case, appended below like any other lede.
    if pub.get("valuation_label") and pub.get("trading_status") != "stale":
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


def render_snapshot(snap, base_url, lang="en", hreflang_alternates=None):
    """snap is snapshot_store.get_snapshot(ticker)'s return value (not
    None - caller handles the unknown-ticker/404 case).

    Fix 6, AI fixes round 2 (2026-08-31): every number below now comes
    through snapshot_store.public_view(data) rather than the raw stored
    row - see that function's own docstring. This page no longer shows
    the LONG/AVOID-style Signal word anywhere (title, H1, lede or meta
    description) - those read as recommendations on an indexable page,
    which conflicts with the site's factual framing and its own
    disclaimer. "Long Score" is now "Value Score" throughout, matching
    the Deep Dive page's own factual-view wording.

    lang/hreflang_alternates (Español completion, Part 3): lang="es"
    translates the page's own chrome (kicker, stat-cell labels/hints via
    _stat_cells, the fallback lede, the percentile/dividend/history
    lines, the "Open the interactive Deep Dive" link and the "Use this
    data programmatically" CTA) using the established glossary terms.
    Ticker symbols, company names, universe names, dates and every
    NUMBER are untouched data either way. The "Copy as text" payload
    (_snapshot_copy_text) deliberately stays English regardless of lang -
    same "copied text is data for sharing" rule Español completion Part 1
    established for the Deep Dive page's own copy-as-text button. lang=
    "en" (the default) renders EXACTLY as before - the existing /s/
    <ticker> route with no lang is byte-identical (this function's
    JSON-LD already carried a hardcoded "en" inLanguage value, so
    threading the real `lang` through it changes nothing for that
    default case)."""
    e = html.escape
    ticker = snap["ticker"]
    data = snap.get("data") or {}
    pub = snapshot_store.public_view(data)
    moat = snap.get("moat")
    universe = snap.get("universe") or ""
    generated = (snap.get("generated_at") or "")[:16].replace("T", " ")
    company_name = pub.get("company_name")
    has_name = bool(company_name) and company_name != ticker

    cells = _stat_cells(pub, moat, lang)
    cell_html = "".join(
        f'<div class="sdd-snap-cell"><div class="lbl">{e(label)}</div>'
        f'<div class="val">{e(str(value))}</div>'
        + (f'<div class="hint">{e(hint)}</div>' if hint else "")
        + "</div>"
        for label, value, hint in cells
    )

    # Commit J (21 Sep 2026, owner-reported): a ticker flagged "stale"
    # (see _not_trading_banner()'s own docstring) gets that banner as
    # its lede INSTEAD OF the normal valuation-based one - the whole
    # point being to suppress the "Undervalued"/"Expensive" claim a
    # frozen ghost price can no longer honestly support. Everything
    # else on the page (stat grid, Deep Dive link, API/MCP CTA) stays
    # exactly as reachable as for any other ticker - only the lede and
    # the FAQ's "Is TICKER undervalued?" question (see _faq_items())
    # change.
    is_stale = pub.get("trading_status") == "stale"
    if is_stale:
        valuation = None
        # Plain escaped text, not wrapped in its own tag here - the CSS
        # class that highlights it as a warning is applied to the <p>
        # itself where `lede` is emitted into `body` below, so this
        # string stays usable as-is for _snapshot_copy_text()'s plain-
        # text payload too (which only strips a couple of known HTML
        # ENTITIES out of `lede`, never arbitrary tags - see that
        # function's own docstring).
        lede = e(_not_trading_banner(ticker, lang))
    else:
        valuation = _valuation_note(pub, lang)
        if lang == "es":
            lede = (f"{e(valuation)} &middot; Calidad {e(_fmt(pub.get('quality')))}/100"
                    if pub.get("valuation_label") else
                    "Puntajes calculados para esta acción, del mismo motor "
                    "detrás de Deep Dive y el Buscador del sitio.")
        else:
            lede = (f"{e(valuation)} &middot; Quality {e(_fmt(pub.get('quality')))}/100"
                    if pub.get("valuation_label") else
                    "Computed scores for this stock, from the same engine behind "
                    "the site's Deep Dive and Scanner pages.")

    hist_line = _score_history_line(ticker, pub.get("value_score"), lang)
    hist_html = f'<p class="sdd-hist-line">{hist_line}</p>' if hist_line else ""

    # Fix (2026-09-02, spec item 7): percentiles already reach this page's
    # own JSON payload but were never shown here - see _percentile_line()'s
    # own docstring.
    pct_line = _percentile_line(pub, universe, lang)
    pct_html = f'<p class="sdd-snap-pct">{pct_line}</p>' if pct_line else ""

    # Services batch 3, Part A1: dividend headline numbers, same public
    # whitelist mechanism as the percentile line right above.
    div_line = _dividend_line(pub, lang)
    div_html = f'<p class="sdd-snap-pct">{div_line}</p>' if div_line else ""

    display_name = f"{ticker} — {company_name}" if has_name else ticker
    if lang == "es":
        title = (f"{display_name}: valor intrínseco, Puntaje Value, Foso | {SITE_NAME}"
                  if has_name else f"{ticker}: puntajes calculados | {SITE_NAME}")
    else:
        title = (f"{display_name}: intrinsic value, Value Score, Moat | {SITE_NAME}"
                  if has_name else f"{ticker} snapshot - computed scores | {SITE_NAME}")
    # citation_url: the visible "Copy as text" citation (below) has always
    # cited the canonical EN URL regardless of lang - untouched here, per
    # this fix's own "zero visual change" scope.
    citation_url = snapshot_url(base_url, ticker)
    # canonical: the <head> <link rel=canonical>/JSON-LD url - this one
    # MUST be self-referencing (priority micro-commit, 18 Sep 2026): the
    # ES page's own canonical is its own /es/s/<ticker> URL, not the EN
    # one - a non-self-referencing canonical tells search engines the ES
    # page isn't the "real" page, which is wrong (it's a real, separately
    # indexable translation). The existing hreflang_alternates (passed in
    # by the caller) already carry both language URLs correctly and are
    # untouched by this fix - hreflang was never the bug, only this tag.
    canonical = f"{base_url}/es/s/{ticker}" if lang == "es" else citation_url
    cite_date = (snap.get("generated_at") or "")[:10]
    if lang == "es":
        citation_text = f'{SITE_NAME}, "{ticker}: resumen,"' + (
            f" {cite_date}" if cite_date else "") + f". {citation_url}"
    else:
        citation_text = f'{SITE_NAME}, "{ticker} snapshot,"' + (
            f" {cite_date}" if cite_date else "") + f". {citation_url}"

    # Fix 4, AI fixes round 1 (2026-08-31): "Copy as text" - see
    # _snapshot_copy_text()'s and blog_render._copy_as_text_html()'s own
    # docstrings. dom_id is ticker-qualified since /s/<ticker> is a
    # distinct page per ticker but the id namespace is still global HTML.
    # The copied text itself always stays English - see this function's
    # own docstring above.
    copy_text = _snapshot_copy_text(
        ticker, pub, moat, generated, universe, lede, base_url,
        company_name=company_name)
    copy_html = blog_render._copy_as_text_html(
        copy_text, dom_id=f"sdd-copytext-{ticker}")

    # SEO Commit E: "Common questions" FAQ block - see _faq_items()'s own
    # docstring. The not-a-recommendation disclaimer is appended to
    # whichever item ends up first (almost always the "undervalued"
    # question, but never assumed - a ticker missing price/intrinsic/MOS
    # data can still have a Value Score or dividend answer, and the
    # disclaimer belongs on whichever one Google/an AI system actually
    # sees first in the markup).
    faq_items = _faq_items(ticker, data, pub, company_name, cite_date, lang)
    if faq_items:
        faq_items[0]["answer"] = faq_items[0]["answer"] + " " + i18n.t("snapshot.faq.disclaimer", lang)
        faq_entries_html = "".join(
            f'<details class="sdd-faq-item"><summary>{e(it["question"])}</summary>'
            f'<p>{e(it["answer"])}</p></details>'
            for it in faq_items
        )
        faq_html = (
            f'<div class="sdd-faq"><h2>{e(i18n.t("snapshot.faq.heading", lang))}</h2>'
            f'{faq_entries_html}</div>'
        )
    else:
        faq_html = ""

    if lang == "es":
        kicker_word = "Resumen"
        cta_heading = "Usa estos datos mediante programación"
        cta_body = (
            'Los números de esta página también están disponibles como JSON en\n'
            f'    <a href="/api/v1/deep-dive/{e(ticker)}">/api/v1/deep-dive/{e(ticker)}</a> -\n'
            '    ver <a href="/api">los documentos de la API</a> para la superficie\n'
            '    completa de solo lectura, incluyendo un servidor MCP para\n'
            '    asistentes de IA.'
        )
        deep_dive_link = f'Abrir el Deep Dive interactivo de {e(ticker)} &rarr;'
    else:
        kicker_word = "Snapshot"
        cta_heading = "Use this data programmatically"
        cta_body = (
            "This page's numbers are also available as JSON at\n"
            f'    <a href="/api/v1/deep-dive/{e(ticker)}">/api/v1/deep-dive/{e(ticker)}</a> -\n'
            '    see <a href="/api">the API docs</a> for the full read-only surface,\n'
            '    including an MCP server for AI assistants.'
        )
        deep_dive_link = f'Open the interactive Deep Dive for {e(ticker)} &rarr;'

    body = f"""
<main><div class="wrap">
  <div class="kicker">{kicker_word} &middot; {e(universe)} &middot; generated {e(generated)} UTC</div>
  <h1>{e(display_name)}</h1>
  <p class="lede{' sdd-snap-stale' if is_stale else ''}">{lede}</p>
  {hist_html}
  {blog_render._copy_citation_html(citation_text)}
  {copy_html}
  <div class="sdd-snap-grid">{cell_html}</div>
  {pct_html}
  {div_html}
  <p><a href="/deep-dive?ticker={e(ticker)}">{deep_dive_link}</a></p>
  <div class="cta">
    <h3>{cta_heading}</h3>
    <p>{cta_body}</p>
  </div>
  {faq_html}
</div></main>
"""
    desc_subject = f"{ticker} ({company_name})" if has_name else ticker
    if lang == "es":
        facts = []
        if pub.get("intrinsic_value") is not None and pub.get("price") is not None:
            facts.append(
                f"valor intrínseco {_fmt(pub['intrinsic_value'])} vs precio {_fmt(pub['price'])}"
                + (f" (MOS {pub['mos_pct']:+.1f}%)" if pub.get("mos_pct") is not None else "")
            )
        if pub.get("value_score") is not None:
            facts.append(f"Puntaje Value {_fmt(pub['value_score'])}")
        if pub.get("quality") is not None:
            facts.append(f"Calidad {_fmt(pub['quality'])}")
        if moat and moat.get("score") is not None:
            facts.append(f"Foso {_fmt(moat['score'])}")
        if facts:
            description = (
                f"{desc_subject}: {', '.join(facts)} - puntajes calculados por "
                f"{SITE_NAME}, descripciones de cálculos, no recomendaciones."
            )
        else:
            description = (
                f"{desc_subject}: puntajes calculados por el motor Deep Dive de "
                f"{SITE_NAME}, actualizado cada noche - descripciones de "
                "cálculos, no recomendaciones."
            )
    else:
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
    webpage_ld = {
        "@context": "https://schema.org",
        "@type": "WebPage",
        "name": title,
        "description": description,
        "url": canonical,
        "isPartOf": {"@type": "WebSite", "name": SITE_NAME, "url": base_url},
        "publisher": blog_render._organization_json_ld(base_url),
        # tickerSymbol added (SEO Commit E) - name/type were already here.
        "about": {"@type": "Corporation", "name": company_name or ticker, "tickerSymbol": ticker},
        "dateModified": (snap.get("generated_at") or "")[:19],
        "inLanguage": lang,
    }
    # SEO Commit E: FAQPage structured data, generated from the EXACT SAME
    # faq_items list the visible <details> block above was built from -
    # never a second derivation, so Google can never see markup that
    # disagrees with what's on the page (the whole point of the "penalizes
    # markup that doesn't match visible content" rule this was built to
    # satisfy). Combined with the pre-existing WebPage block via @graph
    # (one <script> tag, two schema.org types) rather than a second
    # <script> tag - _head() only accepts one json_ld string.
    if faq_items:
        faqpage_ld = {
            "@type": "FAQPage",
            "mainEntity": [
                {
                    "@type": "Question",
                    "name": it["question"],
                    "acceptedAnswer": {"@type": "Answer", "text": it["answer"]},
                }
                for it in faq_items
            ],
        }
        # @context lives once at the graph's top level - not repeated on
        # each node - when wrapping WebPage alongside FAQPage this way.
        webpage_ld_node = {k: v for k, v in webpage_ld.items() if k != "@context"}
        json_ld = blog_render._json_ld({"@context": "https://schema.org", "@graph": [webpage_ld_node, faqpage_ld]})
    else:
        json_ld = blog_render._json_ld(webpage_ld)
    # Part 39 (13 Sep 2026): every /s/<ticker> page used to omit image=
    # here entirely, so EVERY ticker shared the one generic site-wide
    # DEFAULT_OG_IMAGE (_head()'s own fallback - see its docstring) -
    # confirmed by reading _head()'s call sites before this change. The
    # per-ticker card server.py's new /og/<ticker>.png route serves is
    # the SAME snapshot this page already rendered from (`pub`/`snap`
    # above), so this is a zero-new-data-source, zero-network addition.
    og_image = f"{base_url}/og/{ticker}.png"
    head = blog_render._head(
        title, description, canonical, base_url, image=og_image,
        extra_meta=f"<style>{_grid_css()}</style>", json_ld=json_ld,
        hreflang_alternates=hreflang_alternates,
    )
    return blog_render._page(head, body, lang=lang)


def render_snapshot_not_found(base_url, ticker, lang="en"):
    """lang (Español completion, Part 3): this 404-ish page is noindex
    either way, but /es/s/{ticker} for an unrecognised ticker should
    still read as Spanish rather than silently falling back to English
    chrome. lang="en" (the default) is byte-identical to before."""
    if lang == "es":
        body = f"""
<main><div class="wrap">
  <div class="kicker">404</div>
  <h1>No hay resumen para {html.escape(ticker)}</h1>
  <p class="lede">Este ticker todavía no pasó por el escaneo nocturno -
  puede que no esté en un universo cubierto, o que un escaneo todavía no se haya ejecutado para él.</p>
  <p><a href="/es/s/">Explorar acciones cubiertas</a> &nbsp;&middot;&nbsp;
     <a href="/deep-dive?ticker={html.escape(ticker)}&lang=es">Probar el Deep Dive en vivo para {html.escape(ticker)} en su lugar &rarr;</a></p>
</div></main>
"""
        head = blog_render._head(
            f"No hay resumen para {ticker} | {SITE_NAME}",
            "Este ticker todavía no ha sido escaneado.",
            f"{base_url}/es/s/", base_url, noindex=True,
        )
        return blog_render._page(head, body, lang=lang)
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


def render_index(rows, base_url, page=1, per_page=200, lang="en",
                 hreflang_alternates=None):
    """rows: snapshot_store.all_snapshots() output (ticker/universe/
    generated_at only - no per-row scoring, so this stays cheap even at
    a few hundred tickers).

    lang/hreflang_alternates (Español completion, Part 3): lang="es"
    translates the heading/lede/table headers/empty-state text and CTA
    links; ticker symbols, universe names and dates stay as data.
    lang="en" (the default) is byte-identical to before."""
    e = html.escape
    total = len(rows)
    start = (page - 1) * per_page
    page_rows = rows[start:start + per_page]

    if lang == "es":
        s_url = "/es/s"
        if page_rows:
            trs = "".join(
                f'<tr><td><a href="/es/s/{e(r["ticker"])}">{e(r["ticker"])}</a></td>'
                f'<td>{e(r["universe"])}</td>'
                f'<td>{e((r.get("generated_at") or "")[:10])}</td></tr>'
                for r in page_rows
            )
            table = (f'<table class="sdd-snap-table"><thead><tr>'
                     f'<th>Ticker</th><th>Universo</th><th>Actualizado</th>'
                     f'</tr></thead><tbody>{trs}</tbody></table>')
        else:
            table = ('<div class="empty">Todavía no hay resúmenes - el primer '
                     'escaneo nocturno completará esta página.</div>')
        body = f"""
<main><div class="wrap">
  <div class="kicker">StocksDeepDive</div>
  <h1>Resúmenes de acciones</h1>
  <p class="lede">{total} acción{'es' if total != 1 else ''} cubierta{'s' if total != 1 else ''}, calculadas cada noche
  y actualizadas automáticamente - los mismos números detrás de las páginas interactivas
  <a href="/deep-dive?lang=es">Deep Dive</a> y <a href="/scanner?lang=es">Buscador</a>,
  como una página simple por ticker. Ver <a href="/api">la API</a> para acceso JSON.</p>
  {table}
</div></main>
"""
        canonical = f"{base_url}/es/s/"
        json_ld = blog_render._json_ld({
            "@context": "https://schema.org",
            "@type": "CollectionPage",
            "name": f"Resúmenes de acciones | {SITE_NAME}",
            "url": canonical,
            "isPartOf": {"@type": "WebSite", "name": SITE_NAME, "url": base_url},
            "publisher": blog_render._organization_json_ld(base_url),
            "inLanguage": lang,
        })
        head = blog_render._head(
            f"Resúmenes de acciones ({total} cubiertas) | {SITE_NAME}",
            "Puntajes de valor, calidad e impulso calculados cada noche para "
            f"cada acción que {SITE_NAME} escanea - una página simple por ticker.",
            canonical, base_url,
            extra_meta=f"<style>{_grid_css()}</style>", json_ld=json_ld,
            hreflang_alternates=hreflang_alternates,
        )
        return blog_render._page(head, body, lang=lang)

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
        hreflang_alternates=hreflang_alternates,
    )
    return blog_render._page(head, body, lang=lang)
