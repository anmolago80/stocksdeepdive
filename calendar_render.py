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


def _entry_row_html(row):
    e = html.escape
    ticker = row["ticker"]
    tag = "✓ reported" if row["status"] == "reported" else "~ expected"
    tag_cls = "reported" if row["status"] == "reported" else "expected"
    delta_html = ""
    if row["status"] == "reported" and row.get("has_event"):
        b, a = row.get("before_value_score"), row.get("after_value_score")
        if b is not None and a is not None:
            delta = a - b
            delta_html = (
                f'<span class="sdd-cal-delta">Value Score {b:.1f} → {a:.1f} '
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


def _section_html(title, grouped, empty_note):
    if not grouped:
        return f'<div class="sdd-cal-section"><h3>{html.escape(title)}</h3><div class="sdd-cal-empty">{html.escape(empty_note)}</div></div>'
    days = []
    for day, rows in grouped:
        day_label = datetime.strptime(day, "%Y-%m-%d").strftime("%a %d %b")
        rows_html = "".join(_entry_row_html(r) for r in rows)
        days.append(f'<div class="sdd-cal-day"><h4>{html.escape(day_label)}</h4>{rows_html}</div>')
    return f'<div class="sdd-cal-section"><h3>{html.escape(title)}</h3>{"".join(days)}</div>'


def render_calendar_page(base_url, generated_at=None, anchor=None):
    """The full /calendar page: this week, next week, then a flat list
    of the rest of the current month - built fresh from
    build_entries(None) (every watched ticker; no "My tickers" filter -
    this page is public and unauthenticated, same stance /s/ and
    /track-record already take)."""
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

    body_sections = (
        _section_html("This week", this_week, "Nothing reported or expected this week.")
        + _section_html("Next week", next_week, "Nothing expected next week yet.")
        + _section_html("Later this month", later_this_month,
                         "Nothing further expected this month yet.")
    )

    body = f"""
<main><div class="wrap">
  <div class="kicker">StocksDeepDive</div>
  <h1>Results calendar</h1>
  <p class="lede">Every stock this site tracks that has reported, or is
  expected to report, this week or next - grouped by day, with the
  before/after Value Score once a report has been re-analysed.</p>
  <p class="lede" style="font-size:13px;color:#8aa0b8;">Dates from the
  data provider; confirmed dates marked ✓, estimates marked ~.
  Descriptions of calculations, not recommendations - see
  <a href="/methodology">how the scores work</a>.</p>
  {body_sections}
</div></main>
"""
    description = (
        f"{SITE_NAME}'s results calendar - which tracked stocks have "
        "reported or are expected to report this week and next, with "
        "before/after Value Score once available."
    )
    json_ld = blog_render._json_ld({
        "@context": "https://schema.org",
        "@type": "Dataset",
        "name": f"{SITE_NAME} results calendar",
        "description": description,
        "url": canonical,
        "creator": blog_render._organization_json_ld(base_url),
        "publisher": blog_render._organization_json_ld(base_url),
        "license": "https://stocksdeepdive.com/methodology",
        "isAccessibleForFree": True,
        "dateModified": generated_at or "",
    })
    head = blog_render._head(
        f"Results calendar | {SITE_NAME}", description, canonical, base_url,
        extra_meta=f"<style>{_CSS}</style>", json_ld=json_ld,
    )
    return blog_render._page(head, body)
