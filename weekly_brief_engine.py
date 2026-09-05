"""
weekly_brief_engine.py

AI-readiness roadmap Phase 8: the personalised AI weekly brief - sent in
the SAME Sunday-morning slot digest_engine.py has always used (see
scheduler_engine.py's _run_digest, now wired to call this module instead
of digest_engine.run_weekly_digest), composing each recipient's email
with a short Sonnet-drafted paragraph on top of the plain watchlist
re-score table that email has always contained.

WHY A NEW MODULE RATHER THAN EDITING digest_engine.py IN PLACE: the same
reasoning Phase 5's portfolio_watchdog_engine.py already established (a
new module, rather than a change inside nightly_scan.py) - digest_engine.py
stays byte-for-byte untouched and still importable/runnable on its own,
so nothing else that might reference it breaks. scheduler_engine.py's job
dispatch is the only call site that changes (one import + one function
call swapped inside its own _run_digest). digest_engine.py's Mailgun
plumbing (_cfg/is_configured/_send) and its per-ticker table renderer are
NOT imported from here - this module keeps its own copy of that plumbing,
the same deliberate per-module convention digest_engine.py /
announce_engine.py / portfolio_watchdog_engine.py each already follow
(see any one of their own docstrings) - so this module has no import-time
dependency on digest_engine.py at all.

RECIPIENTS: unchanged from the existing digest - every signed-in user with
a non-empty watchlist (watchlist_store.all_users()). Nobody who never
opted into a weekly email starts receiving one just because they happen
to also hold a My Portfolio - that would silently expand who gets mailed,
which this phase was never asked to do. A recipient's OWN portfolios
(portfolio_store.list_portfolios / get_holdings) are read purely to
enrich the content of the email they were already going to receive.

CONTENT, per recipient:
  1. Watchlist moves          - the same attention-lite re-score +
                                 score_history "vs 7 days ago" delta
                                 digest_engine.py has always shown,
                                 recomputed independently here (see above
                                 for why it's not imported).
  2. Portfolio health changes - for every holding in every one of this
                                 user's portfolios, the latest recorded
                                 Health score (portfolio_health_engine.
                                 get_recent_health_runs - already written
                                 by the Holdings/Health & News tabs' own
                                 renders; this job NEVER computes a fresh
                                 Health score itself) vs. the closest
                                 recorded score at or before 7 days ago
                                 (portfolio_health_engine.get_health_as_of,
                                 added this phase). A holding nobody has
                                 opened the Health tab for yet simply has
                                 no run history and is silently skipped -
                                 never a fabricated score.
  3. Relevant new posts        - published posts from the last 7 days
                                 (blog_store.posts_for_ticker) whose
                                 primary_ticker is one of this user's
                                 watchlist or holding tickers.

MATERIALITY / AI SPEND: the plain re-score table is ALWAYS included, never
gated on AI - a user who has come to expect the weekly table every Sunday
keeps getting it even when AI is unavailable (ai_client.available() is
False), the gate denies this one recipient (ai_gate.check() fails closed
on a read error, or a free-tier user is genuinely out of questions for
the day), or nothing material moved this week. The Sonnet-written
paragraph is only drafted - and only costs anything - when at least one
of the three sections above has something worth writing about (see
_has_material_content below); a quiet week costs nothing beyond the plain
table Mailgun already sends for free.

Every AI call goes through the same gate every AI-calling feature in this
codebase uses (ai_gate.check()/record() - see that module's own docstring,
"never let an AI call run without the gate"), checked against the
RECIPIENT's own email/tier - unlike Phase 7's admin-only research-note
drafter (which has no visitor identity to gate against and uses
ai_gate.owner_email() instead), this is a visitor-facing feature like the
Ask boxes, so the visitor's own Free/Plus quota is what applies. A
quota-exhausted recipient simply gets the plain table with no AI
paragraph, never a blocked or failed send.

Sonnet 5 (ai_client.MODEL_SONNET) per the roadmap's model policy - Sonnet
is reserved for this feature and Phase 7's research-note drafting only;
every other AI feature on the site stays on Haiku.
"""

import os
from datetime import datetime, timedelta, timezone

import requests

import ai_client
import ai_gate
import blog_store
import calendar_render
import email_auth
import i18n
import portfolio_health_engine
import portfolio_store
import score_history
import watchlist_store

# Mirrors the site's FACTUAL_MODE, same as digest_engine.py.
FACTUAL_MODE = (os.environ.get("FACTUAL_MODE", "true").strip().lower()
                not in ("false", "0", "no", "off"))

_LOOKBACK_DAYS = 7
_WATCHLIST_MOVE_THRESHOLD = 3.0   # Long Score points, either direction
_HEALTH_MOVE_THRESHOLD = 5.0      # Health score points, either direction


# ---------------------------------------------------------------------
# Mailgun plumbing - own copy, see module docstring for why.
# ---------------------------------------------------------------------

def _cfg():
    domain = os.environ.get("MAILGUN_DOMAIN", "").strip()
    return {
        "api_key": os.environ.get("MAILGUN_API_KEY", "").strip(),
        "domain": domain,
        "from": os.environ.get("MAILGUN_FROM", "").strip()
                or (f"StocksDeepDive <digest@{domain}>" if domain else ""),
        "base_url": (
            os.environ.get("MAILGUN_BASE_URL", "").strip()
            or os.environ.get("MAILGUN_API_BASE_URL", "").strip().removesuffix("/v3")
            or "https://api.mailgun.net"
        ).rstrip("/"),
        "site": os.environ.get("SITE_BASE_URL", "").strip()
                or "https://stocksdeepdive.com",
    }


def is_configured():
    c = _cfg()
    return bool(c["api_key"] and c["domain"])


def _send(to_email, subject, html_body):
    c = _cfg()
    resp = requests.post(
        f"{c['base_url']}/v3/{c['domain']}/messages",
        auth=("api", c["api_key"]),
        data={"from": c["from"], "to": [to_email],
              "subject": subject, "html": html_body},
        timeout=20,
    )
    resp.raise_for_status()
    return True


# ---------------------------------------------------------------------
# Section 1: watchlist moves - own copy of digest_engine.py's row logic,
# extended to carry the raw delta (not just a display string) since the
# AI drafting step below needs the number, not the HTML.
# ---------------------------------------------------------------------

_SIGNAL_STYLE = {
    "STRONG LONG": ("#e9f6ee", "#15803d"),
    "LONG": ("#e9f6ee", "#15803d"),
    "WATCHLIST": ("#fdf5e0", "#8a5a00"),
    "AVOID": ("#fdecec", "#b91c1c"),
}
_TD = "padding:9px 10px;font-family:Arial,Helvetica,sans-serif;font-size:14px;color:#1e293b;border-bottom:1px solid #e2e8f0;"
_TH = "padding:9px 10px;font-family:Arial,Helvetica,sans-serif;font-size:12px;color:#64748b;border-bottom:2px solid #cbd5e1;text-transform:uppercase;letter-spacing:0.5px;"


def _watchlist_rows(tickers, ticker_cache):
    """[{**row, 'delta': float|None}] for every ticker with a live
    re-score this run - delta = Long Score now minus the closest
    score_history row at/before 7 days ago (None if no history that far
    back yet)."""
    out = []
    for t in tickers[:20]:  # same per-user sanity cap digest_engine.py uses
        row = ticker_cache.get(t)
        if not row:
            continue
        delta = None
        try:
            past = score_history.get(t, _LOOKBACK_DAYS)
            current = row.get("Long Score")
            if past and past.get("long_score") is not None and current is not None:
                delta = current - past["long_score"]
        except Exception:
            delta = None
        out.append({**row, "delta": delta})
    out.sort(key=lambda r: r.get("Long Score") or 0, reverse=True)
    return out


def _watchlist_row_html(row, site):
    iv = row.get("Intrinsic Value")
    mos = row.get("MOS %")
    sig = row.get("Signal", "-")
    bg, fg = _SIGNAL_STYLE.get(sig, ("#eef2f6", "#475569"))
    link = f"{site}/deep-dive?ticker={row['Ticker']}"
    iv_cell = f"{iv:,.2f}" if iv else "-"
    mos_cell = f"{mos:+.1f}%" if mos is not None else "-"
    delta = row.get("delta")
    if delta is None:
        delta_text, delta_color = "-", "#64748b"
    else:
        delta_text = f"{delta:+.1f}"
        delta_color = "#15803d" if delta > 0 else "#b91c1c" if delta < 0 else "#64748b"
    cells = (
        "<tr>"
        f"<td align='left' style='{_TD}font-weight:bold;'>"
        f"<a href='{link}' style='color:#0f766e;text-decoration:none;'>{row['Ticker']}</a></td>"
        f"<td align='right' style='{_TD}'>{row['Price']:,.2f}</td>"
        f"<td align='right' style='{_TD}'>{iv_cell}</td>"
        f"<td align='right' style='{_TD}'>{mos_cell}</td>"
        f"<td align='right' style='{_TD}'>{row.get('Long Score', '-')}</td>"
        f"<td align='right' style='{_TD}color:{delta_color};font-weight:bold;'>{delta_text}</td>"
    )
    if not FACTUAL_MODE:
        cells += (
            f"<td align='center' style='{_TD}'>"
            f"<span style='background-color:{bg};color:{fg};padding:3px 10px;"
            f"font-family:Arial,Helvetica,sans-serif;font-size:12px;font-weight:bold;'>"
            f"{sig}</span></td>"
        )
    return cells + "</tr>"


# ---------------------------------------------------------------------
# Section 2: portfolio health changes.
# ---------------------------------------------------------------------

def _user_holdings(email):
    """[(portfolio, ticker), ...] across every one of this user's
    portfolios - fetched once per recipient and reused for both the
    portfolio-health section and the relevant-posts section below."""
    out = []
    try:
        for portfolio in portfolio_store.list_portfolios(email):
            for h in portfolio_store.get_holdings(email, portfolio):
                if h.get("ticker"):
                    out.append((portfolio, h["ticker"]))
    except Exception:
        pass
    return out


def _portfolio_health_changes(email, holdings):
    """[{'portfolio','ticker','latest','past','delta'}] for every holding
    that has BOTH a latest recorded Health run and one at/before 7 days
    ago - a holding with no run history at all (nobody has opened its
    Health tab yet) is silently skipped, never given a fabricated score."""
    out = []
    for portfolio, ticker in holdings:
        try:
            recent = portfolio_health_engine.get_recent_health_runs(email, portfolio, ticker, limit=1)
            latest = recent[-1] if recent else None
            past = portfolio_health_engine.get_health_as_of(email, portfolio, ticker, _LOOKBACK_DAYS)
        except Exception:
            continue
        if latest is None or past is None:
            continue
        out.append({"portfolio": portfolio, "ticker": ticker,
                     "latest": latest, "past": past, "delta": latest - past})
    return out


def _health_row_html(h):
    delta = h["delta"]
    color = "#15803d" if delta > 0 else "#b91c1c" if delta < 0 else "#64748b"
    return (
        "<tr>"
        f"<td style='{_TD}'>{h['ticker']} <span style='color:#94a3b8;'>({h['portfolio']})</span></td>"
        f"<td align='right' style='{_TD}'>{h['latest']}</td>"
        f"<td align='right' style='{_TD}color:{color};font-weight:bold;'>{delta:+.1f}</td>"
        "</tr>"
    )


# ---------------------------------------------------------------------
# Section 3: relevant new posts.
# ---------------------------------------------------------------------

def _relevant_new_posts(tickers):
    """Published posts from the last _LOOKBACK_DAYS days whose
    primary_ticker is one of `tickers` - de-duplicated, newest first."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=_LOOKBACK_DAYS)
    seen = set()
    out = []
    for t in tickers:
        try:
            posts = blog_store.posts_for_ticker(t)
        except Exception:
            continue
        for p in posts:
            if p["id"] in seen:
                continue
            pub = p.get("published_at")
            if not pub:
                continue
            try:
                pub_dt = datetime.fromisoformat(pub.replace("Z", "+00:00"))
                if pub_dt.tzinfo is None:
                    pub_dt = pub_dt.replace(tzinfo=timezone.utc)
            except Exception:
                continue
            if pub_dt < cutoff:
                continue
            seen.add(p["id"])
            out.append(p)
    out.sort(key=lambda p: p.get("published_at") or "", reverse=True)
    return out


def _post_line_html(post, site):
    link = f"{site}/blog/{post['slug']}"
    ticker_note = f" - {post['primary_ticker']}" if post.get("primary_ticker") else ""
    return (
        "<div style='padding:4px 0;'>"
        f"<a href='{link}' style='color:#0f766e;text-decoration:none;font-weight:bold;'>{post['title']}</a>"
        f"<span style='color:#94a3b8;'>{ticker_note}</span></div>"
    )


# ---------------------------------------------------------------------
# AI drafting.
# ---------------------------------------------------------------------

_BRIEF_SYSTEM_PROMPT = """You are drafting a short personalised weekly email intro for a
StocksDeepDive subscriber, using ONLY the facts given below - never invent
a number, a company, an event or a reason not present in the data. This is
a factual description of what the site's own calculators show, not
investment advice: never tell the reader to buy, hold or sell anything,
and never predict what a price or score will do next.

Write 2-4 short paragraphs of plain prose - no headings, no bullet lists -
in a plain-spoken, neutral tone. For every fact you mention, name which
section it came from (e.g. "your watchlist", "your <portfolio name>
portfolio", "a new research note") so the reader can see exactly where
each number is grounded. If a section below is empty or not given, do not
mention it - never say a section is empty either, just omit it entirely.
End with one short sentence pointing the reader to the full numbers
further down the email."""

# Español completion, Part 2: appended to _BRIEF_SYSTEM_PROMPT (same one
# AI call, same ai_gate cost) when the recipient's sign-up language is
# "es" - exactly what the instruction asks for on this email specifically
# ("instruct the model to write the brief in Spanish").
_BRIEF_SYSTEM_PROMPT_ES_SUFFIX = (
    "\n\nWrite your entire response in natural, fluent Spanish (the "
    "reader is a Spanish speaker) - still 2-4 short paragraphs, plain "
    "prose, no headings, no bullet lists."
)


def _brief_user_message(watchlist_rows, health_changes, posts, site):
    parts = []
    wl_material = [r for r in watchlist_rows
                   if r.get("delta") is not None and abs(r["delta"]) >= _WATCHLIST_MOVE_THRESHOLD]
    if wl_material:
        lines = [f"- {r['Ticker']}: Long Score {r.get('Long Score')}, {r['delta']:+.1f} vs 7 days ago"
                 for r in wl_material]
        parts.append("WATCHLIST MOVES (Long Score, vs 7 days ago):\n" + "\n".join(lines))

    hc_material = [h for h in health_changes if abs(h["delta"]) >= _HEALTH_MOVE_THRESHOLD]
    if hc_material:
        lines = [f"- {h['ticker']} in '{h['portfolio']}': Health score {h['latest']}, "
                 f"{h['delta']:+.1f} vs 7 days ago" for h in hc_material]
        parts.append("PORTFOLIO HEALTH CHANGES (0-100 Health score, vs 7 days ago):\n" + "\n".join(lines))

    if posts:
        lines = [f"- \"{p['title']}\" on {p.get('primary_ticker') or 'the site'} ({site}/blog/{p['slug']})"
                 for p in posts]
        parts.append("NEW RESEARCH NOTES THIS WEEK:\n" + "\n".join(lines))

    return "\n\n".join(parts)


def _has_material_content(watchlist_rows, health_changes, posts):
    if any(r.get("delta") is not None and abs(r["delta"]) >= _WATCHLIST_MOVE_THRESHOLD
           for r in watchlist_rows):
        return True
    if any(abs(h["delta"]) >= _HEALTH_MOVE_THRESHOLD for h in health_changes):
        return True
    if posts:
        return True
    return False


def _draft_brief(email, watchlist_rows, health_changes, posts, site, lang="en"):
    """Sonnet-drafted intro paragraph(s), or None if there's nothing
    material to write about, the gate denies this recipient, or the call
    itself fails - every case falls back to the plain table with no AI
    text, never a failed/skipped send.

    lang (Español completion, Part 2): appends _BRIEF_SYSTEM_PROMPT_ES_
    SUFFIX when "es" - same one call, same gate/cost, just asked to
    answer in Spanish."""
    user_message = _brief_user_message(watchlist_rows, health_changes, posts, site)
    if not user_message.strip():
        return None
    try:
        allowed, _msg, _tier = ai_gate.check(email, "weekly_brief")
    except Exception:
        allowed = False
    if not allowed:
        return None
    system_prompt = _BRIEF_SYSTEM_PROMPT + (_BRIEF_SYSTEM_PROMPT_ES_SUFFIX if lang == "es" else "")
    result = ai_client.ask(
        system_prompt,
        f"Draft this week's brief from the facts below.\n\n{user_message}",
        model=ai_client.MODEL_SONNET, max_tokens=600,
    )
    if result.get("input_tokens") or result.get("output_tokens"):
        try:
            ai_gate.record(email, "weekly_brief", result.get("model"),
                            result.get("input_tokens"), result.get("output_tokens"),
                            result.get("cost_usd"))
        except Exception:
            pass
    if not result.get("ok"):
        return None
    return result["text"]


# ---------------------------------------------------------------------
# Email assembly + send.
# ---------------------------------------------------------------------

def _reporting_this_week_block_html(reporting_tickers, site, lang="en"):
    """Services batch 2, Part 4 (2026-09-01): "one factual line" -
    deliberately NOT run through _draft_brief/ai_client at all (per the
    spec's own "data only, no AI" instruction) - a plain, always-the-
    same-shape sentence naming this user's own tickers (watchlist ∪
    portfolio holdings) with a reported-or-expected date this ISO week,
    same "this week" window calendar_render.tickers_reporting_this_week()
    computes for the home page's own strip, so the two can never
    disagree about what "this week" means. Empty string (no block at
    all) when nothing of this user's is reporting this week."""
    if not reporting_tickers:
        return ""
    links = ", ".join(
        f'<a href="{site}/deep-dive?ticker={t}" style="color:#0f766e;text-decoration:none;font-weight:bold;">{t}</a>'
        for t in reporting_tickers
    )
    return f"""
    <tr><td style="padding:14px 28px 4px 28px;font-family:Arial,Helvetica,sans-serif;font-size:14px;color:#1e293b;">
      <b>{i18n.t("email.brief.reporting_this_week", lang)}</b> {links}. <a href="{site}/calendar" style="color:#0f766e;">{i18n.t("email.brief.full_calendar_link", lang)}</a>
    </td></tr>"""


def _email_html(email, watchlist_rows, health_changes, posts, site, ai_text, reporting_tickers=None, lang="en"):
    """lang (Español completion, Part 2): heading/intro/section-heading/
    footer/AI-label chrome. `ai_text` itself is already in Spanish when
    lang=="es" (requested directly from the model - see _draft_brief).
    The watchlist/health data tables (rows and column headers) stay
    untranslated, same deferred policy as digest_engine.py's identical
    table in Part 1/2."""
    body_rows = "".join(_watchlist_row_html(r, site) for r in watchlist_rows)
    date_label = i18n.format_date_dmy(datetime.now(timezone.utc), lang)
    reporting_block = _reporting_this_week_block_html(reporting_tickers, site, lang=lang)

    ai_block = ""
    if ai_text:
        ai_paragraphs = "".join(
            f"<p style='margin:0 0 10px 0;'>{p}</p>"
            for p in ai_text.strip().split("\n\n") if p.strip()
        )
        ai_block = f"""
    <tr><td style="padding:4px 28px 14px 28px;font-family:Arial,Helvetica,sans-serif;font-size:14px;color:#1e293b;line-height:1.55;background-color:#f0fdfa;border-top:1px solid #e2e8f0;border-bottom:1px solid #e2e8f0;">
      <div style="font-size:11px;font-weight:bold;color:#0d9488;text-transform:uppercase;letter-spacing:0.5px;padding:12px 0 8px 0;">{i18n.t("email.ai_label", lang)}</div>
      {ai_paragraphs}
    </td></tr>"""

    health_block = ""
    if health_changes:
        health_rows = "".join(_health_row_html(h) for h in health_changes)
        health_block = f"""
    <tr><td style="padding:16px 28px 4px 28px;font-family:Arial,Helvetica,sans-serif;font-size:15px;font-weight:bold;color:#0f172a;">{i18n.t("email.brief.health_heading", lang)}</td></tr>
    <tr><td style="padding:0 28px 4px 28px;">
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
        <tr><th align="left" style="{_TH}">Holding</th><th align="right" style="{_TH}">Health score</th><th align="right" style="{_TH}">Vs 7 days ago</th></tr>
        {health_rows}
      </table>
    </td></tr>"""

    posts_block = ""
    if posts:
        posts_html = "".join(_post_line_html(p, site) for p in posts)
        posts_block = f"""
    <tr><td style="padding:16px 28px 4px 28px;font-family:Arial,Helvetica,sans-serif;font-size:15px;font-weight:bold;color:#0f172a;">{i18n.t("email.brief.posts_heading", lang)}</td></tr>
    <tr><td style="padding:0 28px 4px 28px;font-family:Arial,Helvetica,sans-serif;font-size:14px;">
      {posts_html}
    </td></tr>"""

    return f"""\
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background-color:#f4f7fa;">
<tr><td align="center" style="padding:24px 12px;">
  <table role="presentation" width="620" cellpadding="0" cellspacing="0" border="0" style="background-color:#ffffff;border:1px solid #e2e8f0;">
    <tr><td style="padding:24px 28px 6px 28px;font-family:Arial,Helvetica,sans-serif;">
      <span style="font-size:22px;font-weight:bold;color:#0f172a;">Stocks</span><span style="font-size:22px;font-weight:bold;color:#0d9488;">DeepDive</span>
      <div style="font-size:15px;color:#334155;padding-top:6px;font-weight:bold;">{i18n.t("email.brief.heading", lang)}</div>
      <div style="font-size:13px;color:#64748b;padding-top:4px;">{i18n.t("email.brief.intro", lang, date=date_label)}</div>
    </td></tr>
    {ai_block}
    {reporting_block}
    <tr><td style="padding:14px 28px 4px 28px;">
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
        <tr>
          <th align="left" style="{_TH}">Ticker</th>
          <th align="right" style="{_TH}">Price</th>
          <th align="right" style="{_TH}">Intrinsic value</th>
          <th align="right" style="{_TH}">MOS</th>
          <th align="right" style="{_TH}">{'Value Score' if FACTUAL_MODE else 'Long Score'}</th>
          <th align="right" style="{_TH}">Vs last week</th>
          {'' if FACTUAL_MODE else f'<th align="center" style="{_TH}">Signal</th>'}
        </tr>
        {body_rows}
      </table>
    </td></tr>
    {health_block}
    {posts_block}
    <tr><td style="padding:16px 28px 24px 28px;font-family:Arial,Helvetica,sans-serif;font-size:11px;color:#94a3b8;line-height:1.6;">
      {i18n.t("email.digest.disclaimer_factual" if FACTUAL_MODE else "email.digest.disclaimer_general", lang)}
      {i18n.t("email.brief.footer_note", lang, email=email)}
    </td></tr>
  </table>
</td></tr>
</table>
"""


def run_weekly_brief(log=print):
    """Send the brief to every user with a non-empty watchlist (same
    recipient list digest_engine.run_weekly_digest has always used).
    Returns {'sent': n, 'skipped': n, 'errors': n}."""
    summary = {"sent": 0, "skipped": 0, "errors": 0}
    if not is_configured():
        log("[weekly_brief] Mailgun not configured (MAILGUN_API_KEY / MAILGUN_DOMAIN) - skipping")
        return summary

    from nightly_scan import analyze_ticker_lite  # deferred: heavy import
    site = _cfg()["site"]

    all_users = watchlist_store.all_users()

    # Same "Audit fix 2.5" fan-out digest_engine.py uses: fetch each
    # DISTINCT watched ticker once for the whole run, not once per
    # subscriber, then fan the already-computed results back out below.
    union_tickers = set()
    for _email, _tickers in all_users:
        union_tickers.update(_tickers[:20])

    ticker_cache = {}
    for t in union_tickers:
        try:
            ticker_cache[t] = analyze_ticker_lite(t)
        except Exception:
            ticker_cache[t] = None

    for email, tickers in all_users:
        try:
            # Español completion, Part 2: per-recipient language, same
            # get_signup_lang(email) pattern as this batch's other
            # senders - drives both the AI system prompt and the
            # surrounding chrome/subject.
            _lang = email_auth.get_signup_lang(email)
            watchlist_rows = _watchlist_rows(tickers, ticker_cache)
            if not watchlist_rows:
                summary["skipped"] += 1
                continue

            holdings = _user_holdings(email)
            health_changes = _portfolio_health_changes(email, holdings)
            relevant_tickers = set(tickers) | {t for _p, t in holdings}
            posts = _relevant_new_posts(relevant_tickers)

            # Services batch 2, Part 4: "data only, no AI" factual line -
            # computed independently of _has_material_content/_draft_brief
            # below, so it appears every week this user has something
            # reporting even on an otherwise-quiet week with no AI text.
            try:
                reporting_tickers = calendar_render.tickers_reporting_this_week(relevant_tickers)
            except Exception:
                reporting_tickers = []

            ai_text = None
            if ai_client.available() and _has_material_content(watchlist_rows, health_changes, posts):
                ai_text = _draft_brief(email, watchlist_rows, health_changes, posts, site, lang=_lang)

            subject = (
                i18n.t("email.brief.subject_ai", _lang) if ai_text
                else i18n.t(
                    "email.digest.subject_factual" if FACTUAL_MODE else "email.digest.subject_signal",
                    _lang,
                )
            )
            _send(email, subject, _email_html(
                email, watchlist_rows, health_changes, posts, site, ai_text,
                reporting_tickers=reporting_tickers, lang=_lang,
            ))
            summary["sent"] += 1
            log(f"[weekly_brief] sent to {email} ({len(watchlist_rows)} stocks"
                f"{', AI brief' if ai_text else ''})")
        except Exception as e:
            summary["errors"] += 1
            log(f"[weekly_brief] {email}: {e}")
    return summary


if __name__ == "__main__":
    run_weekly_brief()
