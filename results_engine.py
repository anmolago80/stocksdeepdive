"""
results_engine.py

Services batch, Part 4: results-day re-analysis. No Anthropic API call
anywhere in this module - every before/after number and every "what
moved" bullet is a computed comparison against this site's own scoring
engines (nightly_scan.analyze_ticker_lite, moat_engine, score_history),
exactly like alert_engine.py and insider_engine.py before it.
results_store.py is pure storage; this module owns evaluation +
notification, same split as alert_store/alert_engine.

TWO JOBS, both called from scheduler_engine.py:

  refresh_earnings_calendar(tickers, log=print)
      WEEKLY. For every watched ticker whose earnings_watch row is
      missing or stale (results_store.stale_watch_tickers), scrapes
      yfinance's earnings calendar (Ticker.get_earnings_dates()) and
      records the most recently REPORTED date, the next EXPECTED date,
      and the last several quarters' "Reported EPS" values
      (results_store.upsert_earnings_dates) - the EPS history is what
      lets the day-after re-analysis below compute a genuine before/
      after EPS TTM without a second network call. Capped per run
      (EARNINGS_WEEKLY_CAP) the same way insider_engine.refresh_universe
      caps its own nightly pass - a full universe rolls through across
      several weekly runs rather than blowing one run's time budget.

  check_results_day(log=print)
      NIGHTLY (every night - cheap: a small local table scan plus one
      live re-analysis per ticker that genuinely reported 1 or 3 days
      ago). For every watched ticker whose last_report_date was exactly
      1 or 3 days ago:
        - day+1: first pass. "before" comes from score_history (the last
          recorded row strictly before the report date - nothing new
          fetched for that half); "after" comes from a fresh
          nightly_scan.analyze_ticker_lite() + moat_engine call, the
          exact same per-ticker scoring every other nightly path uses,
          plus a factually-computed EPS TTM / FCF base pair (see
          _ttm_eps/_fcf_pair below). Stores one results_events row and
          queues one notification to the ticker's followers + active-
          alert holders (never a second notification on the day+3 pass -
          see check_results_day's own comments).
        - day+3: second pass, to catch statements Yahoo ingested late.
          Re-runs the exact same "after" computation and updates the
          SAME results_events row in place. If the freshly-fetched
          cashflow statement still predates the report (see
          _statements_stale below), the row's stale flag stays set so
          the Deep Dive card can caption that these numbers might still
          be running ahead of what Yahoo has ingested.

WHAT MOVED: ranked by absolute PERCENT change (unit-agnostic, so a $
metric and a points metric compete fairly) - never an AI summary, never
"why" it moved, only "what" and "by how much". See what_moved() below.

DATA-SOURCE CAVEAT (flagged again in the batch's own final report): EPS
TTM and FCF base "before" values are reconstructed from the earnings-
calendar scrape's own "Reported EPS" column and from the two most recent
columns of yfinance's cashflow statement, respectively - not from a
point-in-time snapshot taken before the report (this site doesn't
proactively fetch fundamentals for tickers nobody has looked at yet).
Both are only available when enough quarters of history exist; when they
aren't, that row's "before" is shown as N/A rather than guessed, and it's
excluded from "what moved" (a metric needs two points to have "moved").
"""

import os
import time
from datetime import datetime, timezone, date

import pandas as pd
import requests

import results_store
import email_auth
import i18n

AI_FEATURE = None  # this module never calls the Anthropic API - see the
# module docstring; explicit for the same reason alert_engine.py's own
# AI_FEATURE=None is explicit.

EARNINGS_WEEKLY_CAP = 400  # per weekly run - see refresh_earnings_calendar
STALE_WATCH_MAX_AGE_DAYS = 6
EPS_HISTORY_QUARTERS = 8


# --------------------------------------------------------------------
# Metric formatting - shared by what_moved()'s bullet text, the
# notification email, and app.py's before/after card (imported directly
# rather than re-implemented, same reuse convention as alert_engine.
# METRIC_LABELS being public for app.py's My-alerts tab).
# --------------------------------------------------------------------

def fmt_money_compact(v):
    if v is None:
        return "N/A"
    av = abs(v)
    sign = "-" if v < 0 else ""
    if av >= 1e9:
        return f"{sign}${av / 1e9:,.2f}B"
    if av >= 1e6:
        return f"{sign}${av / 1e6:,.2f}M"
    if av >= 1e3:
        return f"{sign}${av / 1e3:,.1f}K"
    return f"{sign}${av:,.0f}"


METRIC_META = {
    "value_score": {"label": "Value Score", "kind": "pts",
                     "fmt": lambda v: f"{v:.1f}"},
    "quality": {"label": "Quality", "kind": "pts",
                "fmt": lambda v: f"{v:.0f}"},
    "moat": {"label": "Moat", "kind": "pts",
             "fmt": lambda v: f"{v:.0f}"},
    "mos_pct": {"label": "MOS", "kind": "pct",
                "fmt": lambda v: f"{v:.1f}%"},
    "intrinsic_value": {"label": "Intrinsic value", "kind": "money",
                         "fmt": lambda v: f"${v:,.2f}"},
    "eps_ttm": {"label": "EPS (TTM)", "kind": "money",
                "fmt": lambda v: f"${v:,.2f}"},
    "fcf_base": {"label": "FCF base", "kind": "money_compact",
                 "fmt": fmt_money_compact},
}

# The order the Deep Dive card's table renders rows in - not the same as
# what_moved()'s ranked order, which is by magnitude of change.
METRIC_ORDER = ["value_score", "quality", "moat", "mos_pct",
                 "intrinsic_value", "eps_ttm", "fcf_base"]


def _delta_text(kind, delta):
    sign = "+" if delta >= 0 else ""
    if kind in ("pts", "pct"):
        return f"{sign}{delta:.1f}pts"
    if kind == "money":
        return f"{sign}${delta:,.2f}"
    if kind == "money_compact":
        return f"{sign}{fmt_money_compact(delta)}"
    return f"{sign}{delta:.1f}"


def what_moved(before, after, top_n=5):
    """Ranked (largest absolute % change first) list of
    {"metric","label","before","after","delta","text"} for every metric
    present in BOTH before and after (a metric with only one side known
    can't have "moved" - see module docstring's data-source caveat).
    Pure function, no I/O - directly unit-testable."""
    items = []
    for key in METRIC_ORDER:
        meta = METRIC_META[key]
        b, a = (before or {}).get(key), (after or {}).get(key)
        if b is None or a is None:
            continue
        delta = a - b
        if delta == 0:
            continue
        rank = abs(delta / b) if b != 0 else abs(delta)
        items.append({
            "metric": key, "label": meta["label"], "before": b, "after": a,
            "delta": delta,
            "text": (f"{meta['label']} moved from {meta['fmt'](b)} to {meta['fmt'](a)} "
                     f"({_delta_text(meta['kind'], delta)})"),
            "_rank": rank,
        })
    items.sort(key=lambda it: it["_rank"], reverse=True)
    for it in items:
        it.pop("_rank")
    return items[:top_n]


# --------------------------------------------------------------------
# Earnings-calendar parsing - pure, no I/O (the DataFrame is fetched by
# the caller so this half stays directly unit-testable against a
# synthetic frame shaped like yfinance's real scrape).
# --------------------------------------------------------------------

def _parse_earnings_dates_df(df, today=None):
    """df: a DataFrame shaped like yfinance's Ticker.get_earnings_dates()
    - one row per earnings date, some date-like column (its index is
    named "Earnings Date" in current yfinance; tolerated as a plain
    column too, and matched case-insensitively by substring so a minor
    yfinance rename doesn't silently break this), and a "Reported EPS"-
    ish column that's null for a future (not-yet-reported) date and a
    number for a past one.

    Returns {"last_report_date", "next_report_date", "eps_history"} -
    the two dates as 'YYYY-MM-DD' strings or None, eps_history a list of
    {"date","reported_eps"} for the EPS_HISTORY_QUARTERS most recent
    REPORTED dates, newest first."""
    empty = {"last_report_date": None, "next_report_date": None, "eps_history": []}
    if df is None or getattr(df, "empty", True):
        return empty
    d = df.reset_index()
    date_col = next((c for c in d.columns if "date" in str(c).lower()), None)
    eps_col = next(
        (c for c in d.columns if str(c).lower().replace(" ", "") == "reportedeps"), None
    )
    if date_col is None:
        return empty
    today = today or datetime.now(timezone.utc).date()
    reported, future = [], []
    for _, r in d.iterrows():
        try:
            day = pd.Timestamp(r[date_col]).date()
        except Exception:
            continue
        eps_val = r.get(eps_col) if eps_col is not None else None
        try:
            eps_val = (float(eps_val)
                       if eps_val is not None and not pd.isna(eps_val) else None)
        except (TypeError, ValueError):
            eps_val = None
        if eps_val is not None and day <= today:
            reported.append({"date": day.isoformat(), "reported_eps": eps_val})
        elif eps_val is None and day > today:
            future.append(day)
    reported.sort(key=lambda x: x["date"], reverse=True)
    return {
        "last_report_date": reported[0]["date"] if reported else None,
        "next_report_date": min(future).isoformat() if future else None,
        "eps_history": reported[:EPS_HISTORY_QUARTERS],
    }


def _ttm_eps(eps_history, offset=0):
    """Trailing-twelve-months EPS from the 4 quarters starting `offset`
    quarters back in `eps_history` (newest-first, from
    _parse_earnings_dates_df) - offset=0 is "as of the most recent
    report" (the "after" side of a results-day comparison), offset=1 is
    the same TTM window shifted back one quarter ("before" that report).
    None if fewer than 4 quarters are available at that offset - a
    partial TTM would be a different, non-comparable number, not a
    smaller-but-valid one."""
    if not eps_history:
        return None
    window = eps_history[offset:offset + 4]
    if len(window) < 4:
        return None
    return round(sum(x["reported_eps"] for x in window), 4)


def _fcf_for_col(cashflow_df, col):
    """Same Operating Cash Flow + Capital Expenditure calc
    portfolio_health_engine.fetch_snapshot() already uses for its own
    fcf_growth figure (capex is already negative in yfinance, so it's
    added, not subtracted) - reused here rather than re-derived
    differently, so this module's FCF base agrees with what the
    Portfolio page would compute for the same column."""
    try:
        ocf = (cashflow_df.loc["Operating Cash Flow", col]
               if "Operating Cash Flow" in cashflow_df.index else None)
        capex = (cashflow_df.loc["Capital Expenditure", col]
                 if "Capital Expenditure" in cashflow_df.index else None)
        if ocf is None or (isinstance(ocf, float) and pd.isna(ocf)):
            return None
        return float(ocf) + float(capex or 0)
    except Exception:
        return None


def _fcf_pair(cashflow_df):
    """(fcf_after, fcf_before) from the two most recent columns of a
    yfinance cashflow statement - "after" is the latest column (which,
    fetched the day after a report, is ordinarily that report's own
    filing), "before" is the column before it. (None, None) if fewer
    than 2 columns exist."""
    if cashflow_df is None or getattr(cashflow_df, "empty", True):
        return None, None
    cols = list(cashflow_df.columns)
    if len(cols) < 2:
        return None, None
    after = _fcf_for_col(cashflow_df, cols[0])
    before = _fcf_for_col(cashflow_df, cols[1])
    return (round(after, 2) if after is not None else None,
            round(before, 2) if before is not None else None)


def _statements_stale(cashflow_df, report_date):
    """True if the most recent column of `cashflow_df` predates
    `report_date` - i.e. Yahoo's cached financial statements haven't
    caught up with this report yet, so the "after" EPS/FCF figures above
    are still computed from pre-report fundamentals even though price/
    valuation already reflect the post-report price. True (flagged) on
    missing/unparseable data too - "can't rule out staleness" is the
    safer default for a red-flag caption than silently not showing one."""
    if cashflow_df is None or getattr(cashflow_df, "empty", True):
        return True
    try:
        latest_date = pd.Timestamp(cashflow_df.columns[0]).date()
        return latest_date < report_date
    except Exception:
        return True


# --------------------------------------------------------------------
# Weekly job: earnings-calendar refresh
# --------------------------------------------------------------------

def refresh_earnings_calendar(tickers, cap=EARNINGS_WEEKLY_CAP,
                               max_age_days=STALE_WATCH_MAX_AGE_DAYS, log=print):
    """WEEKLY. See module docstring. `tickers` is the full watch-list
    (every scanned + every followed ticker) - only the stale subset (up
    to `cap`) is actually fetched this run."""
    import yfinance as yf

    due = results_store.stale_watch_tickers(tickers, max_age_days=max_age_days)
    if not due:
        log("[results_engine] earnings calendar: nothing stale")
        return {"checked": 0, "due": 0}
    due = due[:cap]
    log(f"[results_engine] earnings calendar: refreshing {len(due)}/{len(tickers)} "
        f"stale ticker(s)")
    checked = 0
    for t in due:
        try:
            df = yf.Ticker(t).get_earnings_dates(limit=EPS_HISTORY_QUARTERS + 4)
            parsed = _parse_earnings_dates_df(df)
            results_store.upsert_earnings_dates(
                t, parsed["last_report_date"], parsed["next_report_date"],
                eps_history=parsed["eps_history"],
            )
            checked += 1
        except Exception as e:
            log(f"[results_engine] earnings calendar {t} failed: {e}")
        time.sleep(0.3)  # same Yahoo etiquette as nightly_scan.PER_TICKER_SLEEP
    log(f"[results_engine] earnings calendar: refreshed {checked}/{len(due)}")
    return {"checked": checked, "due": len(due)}


# --------------------------------------------------------------------
# Nightly job: results-day detection + re-analysis
# --------------------------------------------------------------------

def _reanalyze(ticker, report_date, log=print):
    """One fresh re-analysis for `ticker` as of right now - the "after"
    half of a before/after comparison, plus the "before" half read back
    from score_history. Returns (before, after, what_moved, stale) or
    None if the live re-score itself failed (no usable price data - the
    same "one bad ticker never kills the run" stance as nightly_scan)."""
    import nightly_scan
    import score_history
    import yfinance as yf

    row = nightly_scan.analyze_ticker_lite(ticker, attention_lite=True)
    if not row:
        return None
    nightly_scan._attach_moat(row, ticker, log=log)

    try:
        cashflow_df = yf.Ticker(ticker).cashflow
    except Exception:
        cashflow_df = None
    fcf_after, fcf_before = _fcf_pair(cashflow_df)

    watch = results_store.get_earnings_watch(ticker) or {}
    eps_history = watch.get("eps_history") or []
    eps_after = _ttm_eps(eps_history, offset=0)
    eps_before = _ttm_eps(eps_history, offset=1)

    before_hist = score_history.before_date(ticker, report_date.isoformat())
    before = {
        "value_score": (before_hist or {}).get("long_score"),
        "quality": (before_hist or {}).get("quality"),
        "moat": (before_hist or {}).get("moat"),
        "mos_pct": (before_hist or {}).get("mos_pct"),
        "intrinsic_value": (before_hist or {}).get("intrinsic_value"),
        "price": (before_hist or {}).get("price"),
        "eps_ttm": eps_before,
        "fcf_base": fcf_before,
    }
    after = {
        "value_score": row.get("Long Score"), "quality": row.get("Quality"),
        "moat": row.get("Moat"), "mos_pct": row.get("MOS %"),
        "intrinsic_value": row.get("Intrinsic Value"), "price": row.get("Price"),
        "eps_ttm": eps_after, "fcf_base": fcf_after,
    }
    moved = what_moved(before, after)
    stale = _statements_stale(cashflow_df, report_date)
    return before, after, moved, stale


def check_results_day(log=print):
    """NIGHTLY. See module docstring. Idempotent per calendar day: a
    ticker whose day+1 pass already produced a results_events row is
    skipped on a same-day retry; day+3 likewise skips once its update
    has actually landed (pass_count >= 2)."""
    today = datetime.now(timezone.utc).date()
    watched = results_store.all_watched()
    processed, notified = 0, 0
    for w in watched:
        ticker = w["ticker"]
        last_report = w.get("last_report_date")
        if not last_report:
            continue
        try:
            report_date = datetime.strptime(last_report, "%Y-%m-%d").date()
        except ValueError:
            continue
        days_since = (today - report_date).days
        if days_since not in (1, 3):
            continue
        existing = results_store.get_event(ticker, last_report)
        if days_since == 1 and existing:
            continue
        if days_since == 3 and existing and existing.get("pass_count", 0) >= 2:
            continue
        try:
            result = _reanalyze(ticker, report_date, log=log)
        except Exception as e:
            log(f"[results_engine] {ticker} re-analysis failed: {e}")
            continue
        if result is None:
            continue
        before, after, moved, stale = result
        event_id, is_new = results_store.upsert_event(
            ticker, last_report, before, after, moved, stale=stale,
        )
        processed += 1
        log(f"[results_engine] {ticker}: results-day pass (day+{days_since}) "
            + ("recorded [new]" if is_new else "updated"))
        if is_new:
            try:
                sent = _notify_results_event(ticker, last_report, after, moved, log=log)
                if sent:
                    results_store.mark_notified(event_id)
                    notified += sent
            except Exception as e:
                log(f"[results_engine] {ticker} notification failed: {e}")
    return {"processed": processed, "notified": notified}


# --------------------------------------------------------------------
# Notification - same Mailgun shape as alert_engine.py/insider_engine.py
# (no shared notify_engine module exists in this codebase, so this
# follows the established per-module duplication convention).
# --------------------------------------------------------------------

def _cfg():
    domain = os.environ.get("MAILGUN_DOMAIN", "").strip()
    return {
        "api_key": os.environ.get("MAILGUN_API_KEY", "").strip(),
        "domain": domain,
        "from": os.environ.get("MAILGUN_FROM", "").strip()
                or (f"StocksDeepDive <alerts@{domain}>" if domain else ""),
        "base_url": (
            os.environ.get("MAILGUN_BASE_URL", "").strip()
            or os.environ.get("MAILGUN_API_BASE_URL", "").strip().removesuffix("/v3")
            or "https://api.mailgun.net"
        ).rstrip("/"),
        "site": os.environ.get("SITE_BASE_URL", "").strip() or "https://stocksdeepdive.com",
    }


def is_configured():
    c = _cfg()
    return bool(c["api_key"] and c["domain"])


def _send(to_email, subject, html_body):
    c = _cfg()
    resp = requests.post(
        f"{c['base_url']}/v3/{c['domain']}/messages",
        auth=("api", c["api_key"]),
        data={"from": c["from"], "to": [to_email], "subject": subject, "html": html_body},
        timeout=20,
    )
    resp.raise_for_status()
    return True


def _audience(ticker):
    """Every email that should hear about this ticker's results -
    followers (follow_store) union active-alert holders (alert_store) -
    "followers/alert-holders of the ticker get one notification" per
    Part 4's own spec."""
    import follow_store
    import alert_store

    emails = set(follow_store.followers_of(ticker))
    emails.update(a["email"] for a in alert_store.active_alerts_for_ticker(ticker))
    return sorted(emails)


def _event_email_html(ticker, report_date, after, moved, site, lang="en"):
    """lang (Español completion, Part 2): heading/vs-line/footer chrome
    only - each `moved` item's own text is results_engine's own
    engine-generated "what moved" sentence, stays untranslated (same as
    app.py's before/after card in Part 1)."""
    link = f"{site}/deep-dive?ticker={ticker}"
    moved_html = "".join(
        f"<div style='padding:3px 0;'>&bull; {m['text']}</div>" for m in moved
    ) or f"<div style='padding:3px 0;color:#94a3b8;'>{i18n.t('email.results.no_moves', lang)}</div>"
    vs = after.get("value_score")
    vs_line = i18n.t("email.results.vs_line", lang, vs=f"{vs:.1f}") if vs is not None else ""
    reported_on = i18n.t("email.results.reported_on", lang, date=report_date)
    return f"""\
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background-color:#f4f7fa;">
<tr><td align="center" style="padding:24px 12px;">
  <table role="presentation" width="620" cellpadding="0" cellspacing="0" border="0" style="background-color:#ffffff;border:1px solid #e2e8f0;">
    <tr><td style="padding:24px 28px 6px 28px;font-family:Arial,Helvetica,sans-serif;">
      <span style="font-size:22px;font-weight:bold;color:#0f172a;">Stocks</span><span style="font-size:22px;font-weight:bold;color:#0d9488;">DeepDive</span>
      <div style="font-size:15px;color:#334155;padding-top:6px;font-weight:bold;">
        <a href="{link}" style="color:#0f766e;text-decoration:none;">{ticker}</a> {reported_on}
      </div>
      <div style="font-size:13px;color:#334155;padding-top:2px;">{vs_line}</div>
    </td></tr>
    <tr><td style="padding:10px 28px;font-family:Arial,Helvetica,sans-serif;font-size:13.5px;color:#334155;border-top:1px solid #e2e8f0;">
      {moved_html}
    </td></tr>
    <tr><td style="padding:16px 28px 24px 28px;font-family:Arial,Helvetica,sans-serif;font-size:11px;color:#94a3b8;line-height:1.6;">
      {i18n.t("email.results.footer", lang)}
    </td></tr>
  </table>
</td></tr>
</table>
"""


def _notify_results_event(ticker, report_date, after, moved, log=print):
    import push_send

    emails = _audience(ticker)
    if not emails:
        return 0
    c = _cfg()
    email_configured = is_configured()
    push_configured = push_send.is_configured()
    sent = 0
    for email in emails:
        # Español completion, Part 2: per-recipient language, same
        # get_signup_lang(email) pattern as the rest of this batch's
        # senders - the subject/body/push text are all built per
        # recipient now instead of once and reused.
        _lang = email_auth.get_signup_lang(email)
        subject = i18n.t("email.results.subject", _lang, ticker=ticker, date=report_date)
        push_body = moved[0]["text"] if moved else i18n.t(
            "email.results.push_fallback", _lang, ticker=ticker, date=report_date,
        )
        ok = False
        if email_configured:
            try:
                body_html = _event_email_html(ticker, report_date, after, moved, c["site"], lang=_lang)
                _send(email, subject, body_html)
                ok = True
            except Exception as e:
                log(f"[results_engine] email to {email} failed: {e}")
        if push_configured:
            try:
                push_send.send_to_email(
                    email, subject, push_body, url=f"{c['site']}/deep-dive?ticker={ticker}",
                )
                ok = True
            except Exception as e:
                log(f"[results_engine] push to {email} failed: {e}")
        if ok:
            sent += 1
    if sent:
        log(f"[results_engine] {ticker}: notified {sent} recipient(s)")
    return sent
