"""
calendar_render.py

Services batch 2, Part 4 (2026-09-01): the results calendar. One shared
data-builder (build_entries/filter_range/group_by_day/tickers_reporting_
this_week) plus one HTML renderer (render_calendar_page), used by BOTH
the interactive Streamlit /results-calendar page (app.py's
page_results_calendar()) and the indexable, server-rendered /calendar
twin (server.py) - the same "one function, two callers, always word-
identical" convention this codebase already uses for /s/<ticker> (see
snapshot_render.py's own docstring) and /track-record
(track_record_render.py).

DATA SOURCE: entirely results_store.py's own two tables - earnings_watch
(last/next report date per watched ticker) and results_events (the
before/after Value Score a re-analysis pass already computed, when one
exists) - plus snapshot_store.py's cached (universe, Company Name) for
display. No new network call, no engine touched, no Anthropic API call
anywhere in this feature. Nothing here computes a score; every number
shown is read straight back from what nightly_scan/results_engine
already wrote down.

CONFIRMED vs ESTIMATE (the page's own factual caption - "Dates from the
data provider; confirmed dates marked ✓, estimates marked ~"): a REPORTED
date is confirmed - it already happened, and its own EPS is on file
(results_store.upsert_earnings_dates only ever records a past date once
a "Reported EPS" value is scraped for it - see results_engine.
_parse_earnings_dates_df). An EXPECTED (future) date is always an
estimate - it's the data provider's own forecast, not a company-
confirmed date; this site has no separate "company-confirmed" signal to
draw on, so every future date is marked ~ rather than guessing.
"""

import html
from datetime import datetime, timezone, timedelta, date as _date

import blog_render
import i18n
import results_store
import snapshot_store

SITE_NAME = blog_render.SITE_NAME


# --------------------------------------------------------------------
# Data - shared by both callers.
# --------------------------------------------------------------------

def _ticker_display(ticker):
    """(company_name, universe) - best-effort from snapshot_store's
    already-cached scan data (see snapshot_store.save_snapshot, which
    stores the universe a ticker was last scanned in alongside its row).
    Falls back to (ticker, None) for a watched ticker that's never
    actually landed in a snapshot yet (e.g. a portfolio-only holding
    outside every scanned universe, or one seen only via a "*"-style
    imported screen) - never a crash, never a fabricated universe."""
    snap = snapshot_store.get_snapshot(ticker)
    if not snap:
        return ticker, None
    name = (snap.get("data") or {}).get("Company Name") or ticker
    return name, snap.get("universe")


def build_entries(tickers=None):
    """One entry per (ticker, date) this calendar has anything to say
    about - a "reported" entry for each watched ticker's own
    last_report_date (however long ago that was), an "expected" entry
    for each watched ticker's own next_report_date (however far ahead) -
    both read straight off results_store.all_watched(). Deliberately
    does NO date-range filtering itself (see filter_range below) - both
    callers read from this same unfiltered list, so "this week"/"next
    week"/"this month" can never drift between the Streamlit page and
    the server-rendered twin.

    tickers: optional iterable to restrict to (case-insensitive) - used
    by the Streamlit page's "My tickers"/"Universe" filters and by
    tickers_reporting_this_week() below; None (the server-rendered
    page's own "All" view) means every watched ticker."""
    watched = results_store.all_watched()
    if tickers is not None:
        wanted = {t.strip().upper() for t in tickers if t and t.strip()}
        watched = [w for w in watched if w["ticker"] in wanted]

    entries = []
    for w in watched:
        ticker = w["ticker"]
        name, universe = _ticker_display(ticker)
        last_report = w.get("last_report_date")
        if last_report:
            event = results_store.get_event(ticker, last_report)
            entries.append({
                "ticker": ticker, "company_name": name, "universe": universe,
                "date": last_report, "status": "reported", "confirmed": True,
                "before_value_score": ((event or {}).get("before") or {}).get("value_score"),
                "after_value_score": ((event or {}).get("after") or {}).get("value_score"),
                "has_event": event is not None,
            })
        next_report = w.get("next_report_date")
        if next_report:
            entries.append({
                "ticker": ticker, "company_name": name, "universe": universe,
                "date": next_report, "status": "expected", "confirmed": False,
                "before_value_score": None, "after_value_score": None,
                "has_event": False,
            })
    return entries


def filter_range(entries, start, end):
    """Entries with `start` <= date <= `end` (both date objects),
    sorted by date then ticker."""
    s, e = start.isoformat(), end.isoformat()
    return sorted(
        (row for row in entries if s <= row["date"] <= e),
        key=lambda row: (row["date"], row["ticker"]),
    )


def group_by_day(entries):
    """[(date_str, [entries...]), ...], sorted by date - the calendar's
    own "grouped by day" requirement, shared by both renderers."""
    groups = {}
    for row in entries:
        groups.setdefault(row["date"], []).append(row)
    for day_rows in groups.values():
        day_rows.sort(key=lambda row: row["ticker"])
    return sorted(groups.items())


def week_bounds(anchor=None):
    """(monday, sunday) date objects for the ISO week containing
    `anchor` (default: today, UTC)."""
    anchor = anchor or datetime.now(timezone.utc).date()
    monday = anchor - timedelta(days=anchor.weekday())
    return monday, monday + timedelta(days=6)


def month_bounds(anchor=None):
    """(first_day, last_day) date objects for the calendar month
    containing `anchor` (default: today, UTC)."""
    anchor = anchor or datetime.now(timezone.utc).date()
    first = anchor.replace(day=1)
    if first.month == 12:
        next_first = first.replace(year=first.year + 1, month=1)
    else:
        next_first = first.replace(month=first.month + 1)
    return first, next_first - timedelta(days=1)


def tickers_reporting_this_week(tickers):
    """Sorted subset of `tickers` with a reported-or-expected date
    somewhere in the current ISO week - the one factual line the home
    strip (app.py) and the weekly brief (weekly_brief_engine.py) both
    need, kept here so the two can never compute "this week" two
    different ways."""
    if not tickers:
        return []
    monday, sunday = week_bounds()
    entries = filter_range(build_entries(tickers), monday, sunday)
    return sorted({row["ticker"] for row in entries})


# --------------------------------------------------------------------
# Server-rendered HTML (/calendar) - see module docstring. Reuses
# blog_render's page shell/CSS/JSON-LD helpers, same as snapshot_render.py
# and track_record_render.py - no visual pattern invented fresh.
# --------------------------------------------------------------------

_CSS = """
.sdd-cal-day{margin:0 0 18px}
.sdd-cal-day h4{margin:0 0 6px;font-size:13.5px;color:#8aa0b8;font-weight:600}
.sdd-cal-row{display:flex;align-items:baseline;gap:10px;padding:6px 0;
  border-bottom:1px solid #1f3352;flex-wrap:wrap}
.sdd-cal-row a{color:#2dd4bf;font-weight:700;text-decoration:none}
.sdd-cal-tag{font-size:11.5px;padding:1px 8px;border-radius:999px;
  border:1px solid #334155;color:#94a3b8}
.sdd-cal-tag.reported{color:#34d399;border-color:#134e3a}
.sdd-cal-tag.expected{color:#facc15;border-color:#4a3f13}
.sdd-cal-delta{font-size:13px;color:#94a3b8}
.sdd-cal-section{margin:0 0 28px}
.sdd-cal-empty{color:#94a3b8;font-size:14px;padding:6px 0}
"""


def _entry_row_html(row, lang="en"):
    e = html.escape
    ticker = row["ticker"]
    if lang == "es":
        tag = "✓ reportado" if row["status"] == "reported" else "~ previsto"
        vs_word = "Puntaje Value"
    else:
        tag = "✓ reported" if row["status"] == "reported" else "~ expected"
        vs_word = "Value Score"
    tag_cls = "reported" if row["status"] == "reported" else "expected"
    delta_html = ""
    if row["status"] == "reported" and row.get("has_event"):
        b, a = row.get("before_value_score"), row.get("after_value_score")
        if b is not None and a is not None:
            delta = a - b
            delta_html = (
                f'<span class="sdd-cal-delta">{vs_word} {b:.1f} → {a:.1f} '
                f'({delta:+.1f})</span>'
            )
    universe_html = f'<span class="sdd-cal-delta">{e(row["universe"])}</span>' if row.get("universe") else ""
    return (
        '<div class="sdd-cal-row">'
        f'<a href="/deep-dive?ticker={e(ticker)}">{e(ticker)}</a>'
        f'<span>{e(row.get("company_name") or ticker)}</span>'
        f'{universe_html}'
        f'<span class="sdd-cal-tag {tag_cls}">{tag}</span>'
        f'{delta_html}'
        "</div>"
    )


def _section_html(title, grouped, empty_note, lang="en"):
    if not grouped:
        return f'<div class="sdd-cal-section"><h3>{html.escape(title)}</h3><div class="sdd-cal-empty">{html.escape(empty_note)}</div></div>'
    days = []
    for day, rows in grouped:
        day_label = i18n.format_date_a_d_b(datetime.strptime(day, "%Y-%m-%d"), lang)
        rows_html = "".join(_entry_row_html(r, lang) for r in rows)
        days.append(f'<div class="sdd-cal-day"><h4>{html.escape(day_label)}</h4>{rows_html}</div>')
    return f'<div class="sdd-cal-section"><h3>{html.escape(title)}</h3>{"".join(days)}</div>'


def render_calendar_page(base_url, generated_at=None, anchor=None, lang="en",
                          hreflang_alternates=None):
    """The full /calendar page: this week, next week, then a flat list
    of the rest of the current month - built fresh from
    build_entries(None) (every watched ticker; no "My tickers" filter -
    this page is public and unauthenticated, same stance /s/ and
    /track-record already take).

    lang/hreflang_alternates (Español completion, Part 3): same pattern
    as blog_render.render_content_page - lang="es" swaps the page's own
    copy (heading, ledes, section titles/empty notes, entry tags and the
    "Value Score" word) for hand-written Spanish inline, matching that
    function's own established style for this kind of standalone page,
    rather than routing through new i18n.py dict keys. Ticker symbols,
    company names, universes and the before/after score NUMBERS
    themselves are untranslated data, unaffected by lang either way.
    lang="en" (the default) renders EXACTLY as before - every existing
    caller (the /calendar route with no lang, and the Streamlit results-
    calendar page via _section_html/_entry_row_html directly) is
    byte-identical."""
    canonical = f"{base_url}/calendar"
    entries = build_entries(None)

    this_mon, this_sun = week_bounds(anchor)
    next_mon, next_sun = this_mon + timedelta(days=7), this_sun + timedelta(days=7)
    month_first, month_last = month_bounds(anchor)
    later_start = next_sun + timedelta(days=1)

    this_week = group_by_day(filter_range(entries, this_mon, this_sun))
    next_week = group_by_day(filter_range(entries, next_mon, next_sun))
    later_this_month = (
        group_by_day(filter_range(entries, later_start, month_last))
        if later_start <= month_last else []
    )

    if lang == "es":
        heading = "Calendario de resultados"
        lede1 = (
            "Cada acción que este sitio sigue y que ya reportó, o se "
            "espera que reporte, esta semana o la próxima — agrupadas "
            "por día, con el Puntaje Value antes/después una vez que un "
            "informe fue reanalizado."
        )
        lede2_html = (
            'Las fechas provienen del proveedor de datos; las fechas '
            'confirmadas están marcadas con ✓, las estimadas con ~. '
            'Descripciones de cálculos, no recomendaciones — mira '
            '<a href="/es/methodology">cómo funcionan los puntajes</a>.'
        )
        sec_this_week = _section_html("Esta semana", this_week,
                                       "Nada reportado ni previsto esta semana.", lang)
        sec_next_week = _section_html("Próxima semana", next_week,
                                       "Nada previsto para la próxima semana todavía.", lang)
        sec_later = _section_html("Más adelante este mes", later_this_month,
                                   "Nada más previsto este mes todavía.", lang)
        description = (
            f"Calendario de resultados de {SITE_NAME} — qué acciones "
            "seguidas ya reportaron o se espera que reporten esta semana "
            "y la próxima, con el Puntaje Value antes/después cuando "
            "está disponible."
        )
        page_title = f"Calendario de resultados | {SITE_NAME}"
        json_name = f"{SITE_NAME} calendario de resultados"
    else:
        heading = "Results calendar"
        # Original literal line breaks preserved exactly (not just the
        # words) so the EN page stays byte-identical to before this
        # function grew a lang parameter.
        lede1 = (
            "Every stock this site tracks that has reported, or is\n"
            "  expected to report, this week or next - grouped by day, with the\n"
            "  before/after Value Score once a report has been re-analysed."
        )
        lede2_html = (
            'Dates from the\n'
            '  data provider; confirmed dates marked ✓, estimates marked ~.\n'
            '  Descriptions of calculations, not recommendations - see\n'
            '  <a href="/methodology">how the scores work</a>.'
        )
        sec_this_week = _section_html("This week", this_week,
                                       "Nothing reported or expected this week.", lang)
        sec_next_week = _section_html("Next week", next_week,
                                       "Nothing expected next week yet.", lang)
        sec_later = _section_html("Later this month", later_this_month,
                                   "Nothing further expected this month yet.", lang)
        description = (
            f"{SITE_NAME}'s results calendar - which tracked stocks have "
            "reported or are expected to report this week and next, with "
            "before/after Value Score once available."
        )
        page_title = f"Results calendar | {SITE_NAME}"
        json_name = f"{SITE_NAME} results calendar"

    body_sections = sec_this_week + sec_next_week + sec_later

    body = f"""
<main><div class="wrap">
  <div class="kicker">StocksDeepDive</div>
  <h1>{heading}</h1>
  <p class="lede">{lede1}</p>
  <p class="lede" style="font-size:13px;color:#8aa0b8;">{lede2_html}</p>
  {body_sections}
</div></main>
"""
    _dataset = {
        "@context": "https://schema.org",
        "@type": "Dataset",
        "name": json_name,
        "description": description,
        "url": canonical,
        "creator": blog_render._organization_json_ld(base_url),
        "publisher": blog_render._organization_json_ld(base_url),
        "license": "https://stocksdeepdive.com/methodology",
        "isAccessibleForFree": True,
        "dateModified": generated_at or "",
    }
    if lang == "es":
        # Only added for the ES twin - the original EN JSON-LD never had
        # an inLanguage field, and adding one unconditionally would break
        # this page's byte-identical EN output (unlike render_content_page,
        # whose EN JSON-LD already carried a hardcoded "en" value here).
        _dataset["inLanguage"] = lang
    json_ld = blog_render._json_ld(_dataset)
    head = blog_render._head(
        page_title, description, canonical, base_url,
        extra_meta=f"<style>{_CSS}</style>", json_ld=json_ld,
        hreflang_alternates=hreflang_alternates,
    )
    return blog_render._page(head, body, lang=lang)
