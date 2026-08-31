"""
track_record_render.py

Server-rendered HTML for /track-record - AI-readiness roadmap Phase 4
(AI_ROADMAP_stocksdeepdive.md): "a dated past-tense track-record page",
one of the phase's citation helpers alongside llms.txt and the
author/organisation schema now on every page (see blog_render.py).

WHAT THIS IS: for every ticker the nightly scan has tracked for at least
score_history.tracked_summary()'s min_days, this page shows the FIRST day
that ticker was recorded, the Long Score and price the site computed for
it THEN, and the most recent recorded day, score and price - entirely
sourced from score_history.py's own nightly-written rows (see
nightly_scan.py's score_history.record() call after every scan). Nothing
here is computed by this module; it only formats what the engine already
wrote down, on the calendar dates it wrote it.

WHAT THIS DELIBERATELY IS NOT, matching app.py's page_model_history() (the
site's other "history" page) and the roadmap's own non-negotiable ground
rule ("factual framing everywhere ... descriptions of calculations, never
advice"): this is not a track record of investment returns, not a list of
buy/sell calls, and not a claim that any past or future computed score is
accurate. Long Score is a weighted calculation from stated inputs, not a
recommendation - see /methodology. The page says so, prominently, in its
own words rather than reusing page_model_history()'s copy verbatim (the
ground rule protects that page's own prose; this is new prose making the
same point for a different page).

Reuses blog_render's page shell/CSS/JSON-LD/copy-citation helpers, same
as snapshot_render.py - no visual or structural pattern invented fresh.
"""

import html

import blog_render

SITE_NAME = blog_render.SITE_NAME

MIN_DAYS = 60  # matches score_history.tracked_summary()'s own default


def _fmt_score(v):
    return f"{v:.1f}" if isinstance(v, (int, float)) else "-"


def _fmt_price(v):
    return f"{v:,.2f}" if isinstance(v, (int, float)) else "-"


def _pct_change(first, last):
    if not isinstance(first, (int, float)) or not isinstance(last, (int, float)) or first == 0:
        return None
    return (last - first) / first * 100.0


_CSS = """
.sdd-tr-table{width:100%;border-collapse:collapse;font-size:14.5px;margin:0 0 24px}
.sdd-tr-table th,.sdd-tr-table td{padding:8px 10px;border-bottom:1px solid #1f3352;
  text-align:right;white-space:nowrap}
.sdd-tr-table th:first-child,.sdd-tr-table td:first-child{text-align:left}
.sdd-tr-table th{color:#8aa0b8;font-weight:600;font-size:12.5px}
.sdd-tr-table a{color:#2dd4bf}
.sdd-tr-up{color:#34d399}
.sdd-tr-down{color:#fb7185}
.sdd-tr-wrap{overflow-x:auto;margin:0 0 24px}
"""


def render_track_record(rows, base_url, generated_at=None):
    """rows: score_history.tracked_summary() output."""
    e = html.escape
    canonical = f"{base_url}/track-record"

    trs = []
    for r in rows:
        ticker = r["ticker"]
        pct = _pct_change(r.get("first_price"), r.get("last_price"))
        if pct is None:
            pct_html = "-"
        else:
            cls = "sdd-tr-up" if pct >= 0 else "sdd-tr-down"
            pct_html = f'<span class="{cls}">{pct:+.1f}%</span>'
        trs.append(
            "<tr>"
            f'<td><a href="/deep-dive?ticker={e(ticker)}">{e(ticker)}</a></td>'
            f'<td>{e(r.get("first_day") or "-")}</td>'
            f'<td>{_fmt_score(r.get("first_score"))}</td>'
            f'<td>{_fmt_price(r.get("first_price"))}</td>'
            f'<td>{e(r.get("last_day") or "-")}</td>'
            f'<td>{_fmt_score(r.get("last_score"))}</td>'
            f'<td>{_fmt_price(r.get("last_price"))}</td>'
            f'<td>{pct_html}</td>'
            "</tr>"
        )

    if trs:
        table = (
            '<div class="sdd-tr-wrap"><table class="sdd-tr-table"><thead><tr>'
            "<th>Ticker</th><th>First recorded</th><th>Score then</th>"
            "<th>Price then</th><th>Latest recorded</th><th>Score now</th>"
            "<th>Price now</th><th>Price change</th>"
            "</tr></thead><tbody>" + "".join(trs) + "</tbody></table></div>"
        )
    else:
        table = (
            '<div class="empty">Nothing has enough recorded history yet - '
            f"a ticker needs to appear in the nightly scan for at least "
            f"{MIN_DAYS} days before it shows up here.</div>"
        )

    citation_text = (
        f'{SITE_NAME}, "Track record" (data as recorded by the nightly scan). '
        f"{canonical}"
    )

    body = f"""
<main><div class="wrap">
  <div class="kicker">StocksDeepDive</div>
  <h1>Track record</h1>
  <p class="lede">The first and most recent Long Score and price the
  nightly scan has recorded for every stock it has tracked for at least
  {MIN_DAYS} days - dated, and never rewritten after the fact.</p>
  <div class="cta">
    <h3>What this is, and isn't</h3>
    <p>Every row below is exactly what {SITE_NAME}'s engine computed on the
    date shown, and the closing price recorded that day - nothing here is
    reconstructed or restated with hindsight. <b>This is not a record of
    buy/sell calls, a claim about investment performance, or a claim that
    any past or future Long Score is accurate.</b> Long Score is a
    weighted calculation from stated inputs, not a recommendation - see
    <a href="/methodology">how the scores work</a>. A stock's price moving
    since it was first recorded says nothing about whether the score at
    the time was "right"; it is shown only because it is the one fact
    about "what actually happened afterwards" that can be stated without
    any interpretation at all.</p>
  </div>
  {blog_render._copy_citation_html(citation_text)}
  {table}
</div></main>
"""

    description = (
        "A dated, past-tense record of the Long Score and price "
        f"{SITE_NAME} recorded for each tracked stock, first and most "
        "recently - not a claim about recommendation accuracy or "
        "investment performance."
    )
    json_ld = blog_render._json_ld({
        "@context": "https://schema.org",
        "@type": "Dataset",
        "name": f"{SITE_NAME} track record",
        "description": description,
        "url": canonical,
        "creator": blog_render._organization_json_ld(base_url),
        "publisher": blog_render._organization_json_ld(base_url),
        "license": "https://stocksdeepdive.com/methodology",
        "isAccessibleForFree": True,
        "dateModified": generated_at or "",
        "variableMeasured": ["Long Score", "Price"],
    })
    head = blog_render._head(
        f"Track record | {SITE_NAME}", description, canonical, base_url,
        extra_meta=f"<style>{_CSS}</style>", json_ld=json_ld,
    )
    return blog_render._page(head, body)
