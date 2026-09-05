"""
portfolio_watchdog_engine.py

AI-readiness roadmap Phase 5 (AI_ROADMAP_stocksdeepdive.md): the nightly
Portfolio AI watchdog. For every portfolio a user has opted into it (My
Portfolio -> Holdings -> Portfolio settings -> "AI watchdog"), re-scores
each holding the same way app.py's _analyze_holding() does (same
portfolio_health_engine / portfolio_news_engine calls, same composition -
this module can't import app.py itself, since app.py runs Streamlit
commands at module import time and is not a plain-importable module
outside a live Streamlit session), and - ONLY when something MATERIAL
changed since the last time this holding was checked - drafts a short,
factual "what changed" brief with Claude Haiku 4.5 and a one-line
thesis-check against the holding's own saved thesis text, then delivers
it by email and push. "Material" means portfolio_news_engine's own
materiality flag, the Health score moving past HEALTH_DELTA_THRESHOLD, or
the holding flipping to thesis-breaking - never just "a night went by."

Runs from scheduler_engine.py's nightly loop, NOT a live browser session,
so every per-user AI call goes through ai_gate.check()/record() exactly
as the Deep Dive and My Portfolio Ask boxes already do (ai_gate.py is
plain Python with no Streamlit dependency for precisely this reason - see
its own docstring, which already names this module by intent). A user's
quota is the SAME shared pool their live Ask-box use draws from - the
watchdog is one more caller of that one gate, not a separate budget, per
Phase 3's own design decision to keep exactly one pool across every AI
feature. Practically: a night with many material holdings could use up a
chunk of a Free user's daily 20-question quota before they wake up -
MAX_BRIEFS_PER_USER_PER_NIGHT bounds how much of one night's run any
single user can spend, but the shared-pool trade-off itself is a known,
deliberate consequence of that earlier design choice, not an oversight.

DEDUPLICATION: a small state table here (portfolio_watchdog_state) keeps
one signature string per (email, portfolio, ticker), built from the
current Health score, News risk score and the sorted set of "today"
news headlines - so the exact same unchanged material condition never
re-fires every night for the 3-day window portfolio_news_engine's own
"today" list naturally spans. A new signature always means at least one
of those numbers actually moved; an unchanged one is silently skipped,
no AI call made, no notification sent.

Every AI-written paragraph in the resulting email/push is labelled with
ai_client.ANSWER_LABEL, per the roadmap's own non-negotiable rule that
every AI-written block be visibly labelled.
"""

import os
import sqlite3
from datetime import datetime, timezone

import requests

import ai_client
import ai_gate
import email_auth
import i18n
import portfolio_health_engine
import portfolio_news_engine
import portfolio_store
import push_send

# A Health-score move (either direction) at least this large counts as
# material on its own, independent of portfolio_news_engine's own
# materiality flag and the thesis-breaking flag - so a holding can trigger
# a brief purely on a quality/valuation-driven Health swing with no fresh
# news attached at all.
HEALTH_DELTA_THRESHOLD = 6.0

# A fast circuit breaker independent of ai_gate's own daily/monthly quota:
# never let one user's portfolio spend more than this many AI calls in one
# night, however many holdings changed - so a user with 40 holdings that
# all moved on the same news day doesn't burn their whole day's quota (or
# contribute unboundedly to the site-wide spend cap) in a single run.
MAX_BRIEFS_PER_USER_PER_NIGHT = 8

AI_FEATURE = "portfolio_watchdog"


def _data_dir():
    return os.environ.get("RAILWAY_VOLUME_MOUNT_PATH") or os.path.dirname(__file__)


DB_PATH = os.path.join(_data_dir(), "stocksdeepdive.db")


def _conn():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS portfolio_watchdog_state (
            email TEXT NOT NULL,
            portfolio TEXT NOT NULL,
            ticker TEXT NOT NULL,
            signature TEXT NOT NULL,
            last_notified_at TEXT NOT NULL,
            PRIMARY KEY (email, portfolio, ticker)
        )"""
    )
    return conn


def _get_last_signature(email, portfolio, ticker):
    with _conn() as conn:
        row = conn.execute(
            "SELECT signature FROM portfolio_watchdog_state "
            "WHERE email = ? AND portfolio = ? AND ticker = ?",
            (email, portfolio, ticker),
        ).fetchone()
    return row[0] if row else None


def _record_signature(email, portfolio, ticker, signature):
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as conn:
        conn.execute(
            "INSERT INTO portfolio_watchdog_state "
            "(email, portfolio, ticker, signature, last_notified_at) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT(email, portfolio, ticker) DO UPDATE SET "
            "signature = excluded.signature, last_notified_at = excluded.last_notified_at",
            (email, portfolio, ticker, signature, now),
        )


def get_last_notified(email, portfolio, ticker):
    """ISO timestamp of the last brief actually sent for this holding, or
    None - read-only, used by app.py to show a "last watchdog brief"
    caption next to the thesis editor. Never writes."""
    if not email or not portfolio or not ticker:
        return None
    with _conn() as conn:
        row = conn.execute(
            "SELECT last_notified_at FROM portfolio_watchdog_state "
            "WHERE email = ? AND portfolio = ? AND ticker = ?",
            (email, portfolio, ticker.upper()),
        ).fetchone()
    return row[0] if row else None


# -----------------------------------
# Mailgun send - identical config/call shape to digest_engine.py and
# announce_engine.py (same Mailgun account, own "from" address).
# -----------------------------------

def _cfg():
    domain = os.environ.get("MAILGUN_DOMAIN", "").strip()
    return {
        "api_key": os.environ.get("MAILGUN_API_KEY", "").strip(),
        "domain": domain,
        "from": os.environ.get("MAILGUN_FROM", "").strip()
                or (f"StocksDeepDive <watchdog@{domain}>" if domain else ""),
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


def _brief_html(ticker, link, ai_text, lang="en"):
    return f"""
<tr><td style="padding:14px 28px 4px 28px;font-family:Arial,Helvetica,sans-serif;">
  <div style="font-size:15px;font-weight:bold;color:#0f172a;">
    <a href="{link}" style="color:#0f766e;text-decoration:none;">{ticker}</a>
  </div>
  <div style="font-size:13.5px;color:#334155;line-height:1.6;padding-top:6px;white-space:pre-wrap;">{ai_text}</div>
  <div style="font-size:11px;color:#94a3b8;padding-top:6px;">{i18n.t("email.ai_label", lang)}</div>
</td></tr>
"""


def _email_html(briefs, site, lang="en"):
    """lang (Español completion, Part 2): heading/intro/footer/AI-label
    chrome. Each brief's own AI-written text is requested directly in
    Spanish from the model when lang=="es" (see _SYSTEM_PROMPT/
    _build_prompt below) rather than translated after the fact."""
    body_rows = "".join(
        _brief_html(b["ticker"], f"{site}/deep-dive?ticker={b['ticker']}", b["text"], lang=lang)
        for b in briefs
    )
    date_label = i18n.format_date_dmy(datetime.now(timezone.utc), lang)
    n = len(briefs)
    heading = i18n.t(
        "email.watchdog.heading_one" if n == 1 else "email.watchdog.heading_many", lang, n=n,
    )
    return f"""\
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background-color:#f4f7fa;">
<tr><td align="center" style="padding:24px 12px;">
  <table role="presentation" width="620" cellpadding="0" cellspacing="0" border="0" style="background-color:#ffffff;border:1px solid #e2e8f0;">
    <tr><td style="padding:24px 28px 6px 28px;font-family:Arial,Helvetica,sans-serif;">
      <span style="font-size:22px;font-weight:bold;color:#0f172a;">Stocks</span><span style="font-size:22px;font-weight:bold;color:#0d9488;">DeepDive</span>
      <div style="font-size:15px;color:#334155;padding-top:6px;font-weight:bold;">{heading}</div>
      <div style="font-size:13px;color:#64748b;padding-top:4px;">{i18n.t("email.watchdog.intro", lang, date=date_label)}</div>
    </td></tr>
    {body_rows}
    <tr><td style="padding:16px 28px 24px 28px;font-family:Arial,Helvetica,sans-serif;font-size:11px;color:#94a3b8;line-height:1.6;">
      {i18n.t("email.watchdog.footer", lang)}
    </td></tr>
  </table>
</td></tr>
</table>
"""


# -----------------------------------
# Per-holding analysis - mirrors app.py's _analyze_holding() composition.
# That function itself has no Streamlit dependency, but app.py as a whole
# cannot be imported outside a live Streamlit run (st.set_page_config() at
# module level), so the same few engine calls are repeated here rather
# than imported. Deliberately omits _dcf_overrides_for()/manual IV
# override lookups app.py's live page also threads through - an
# admin/user DCF override changes what the interactive page displays, not
# the nightly factual signal this job cares about (Health score direction,
# materiality, thesis fit).
# -----------------------------------

def _analyze_holding(h):
    is_etf = (h.get("kind") or "STOCK").upper() == "ETF"
    snap = portfolio_health_engine.fetch_snapshot(h["ticker"])
    try:
        news = portfolio_news_engine.analyze_holding_news(
            h["ticker"], name=h.get("name"), thesis_drivers=h.get("thesis_drivers"),
            buy_date=h.get("buy_date"), is_etf=is_etf,
        )
    except Exception:
        news = None
    components = portfolio_health_engine.compute_health_components(
        snap, h.get("kind"), baseline=h.get("baseline"), buy_date=h.get("buy_date"), news=news,
    )
    progress = portfolio_health_engine.compute_progress(
        snap, h.get("baseline"), h.get("kind"), h.get("buy_price"), buy_date=h.get("buy_date"),
    )
    health = portfolio_health_engine.compute_health(
        components, news=news, is_etf=is_etf, progress_overall=progress.get("overall"),
    )
    return {"snapshot": snap, "news": news, "health": health, "is_etf": is_etf}


def _signature(health, news):
    overall = health.get("overall")
    risk = (news or {}).get("news_risk_score")
    today_keys = sorted(
        f"{(e.get('date').isoformat() if e.get('date') else 'undated')}::{e.get('title', '')}"
        for e in ((news or {}).get("today") or [])
    )
    return f"{overall}|{risk}|{'|'.join(today_keys)}"


def _is_material(health, news, prev_overall):
    if news and news.get("material"):
        return True
    if health.get("thesis_breaking"):
        return True
    if prev_overall is not None and health.get("overall") is not None:
        if abs(health["overall"] - prev_overall) >= HEALTH_DELTA_THRESHOLD:
            return True
    return False


_SYSTEM_PROMPT = (
    "You write short, factual 'what changed' briefs for one stock a "
    "reader already owns, using ONLY the data given to you below - never "
    "invented numbers, never a buy/hold/sell recommendation, never advice "
    "of any kind. State plainly what moved (health score, news, price) "
    "and, if the reader's own saved thesis is given, add one sentence "
    "noting whether anything below appears to support or challenge that "
    "thesis - as a factual observation, not a verdict. 3-5 sentences, "
    "plain language, no bullet points, no headers."
)

# Español completion, Part 2: appended to _SYSTEM_PROMPT (same one AI
# call, same ai_gate cost) when the recipient's sign-up language is "es" -
# same "instruct the model to write in Spanish" approach the instruction
# asks for on the weekly brief, applied here too so this email's AI
# paragraph isn't the one English block on an otherwise-Spanish page.
_SYSTEM_PROMPT_ES_SUFFIX = (
    " Write your entire response in natural, fluent Spanish (the reader "
    "is a Spanish speaker) - still 3-5 sentences, plain language, no "
    "bullet points, no headers."
)


def _build_prompt(ticker, h, analysis, prev_overall):
    health = analysis["health"]
    news = analysis["news"] or {}
    lines = [f"Ticker: {ticker}"]
    if h.get("thesis"):
        lines.append(f"Investor's saved thesis: {h['thesis']}")
    overall_line = f"Health score now: {health.get('overall')}/100"
    if prev_overall is not None:
        overall_line += f" (was {prev_overall}/100 at last check)"
    lines.append(overall_line)
    if health.get("action"):
        lines.append(f"Health verdict: {health['action']}")
    if health.get("thesis_breaking"):
        lines.append("Flag: thesis-threatening news detected.")
    if news.get("today"):
        lines.append("Recent relevant news:")
        for e in news["today"][:5]:
            d = e["date"].strftime("%Y-%m-%d") if e.get("date") else "undated"
            lines.append(f"- [{d}] {e.get('title')} (severity: {e.get('severity')})")
    return "\n".join(lines)


def run_nightly_watchdog(log=print):
    """Send each opted-in user one combined email (+ best-effort push) for
    every holding with a material change since its last recorded
    signature. Returns {'users_notified', 'briefs_sent',
    'skipped_no_change', 'gate_blocked', 'errors'}."""
    summary = {"users_notified": 0, "briefs_sent": 0, "skipped_no_change": 0,
               "gate_blocked": 0, "errors": 0}
    if not ai_client.available():
        log("[watchdog] AI features not configured (ANTHROPIC_API_KEY) - skipping")
        return summary

    owners = portfolio_store.list_portfolio_owners()
    if not owners:
        log("[watchdog] no portfolios have the AI watchdog enabled - nothing to do")
        return summary

    site = _cfg()["site"]
    push_configured = push_send.is_configured()
    email_configured = is_configured()

    for email, portfolio in owners:
        try:
            # Español completion, Part 2: resolved once per user (a
            # first-touch-only column - see email_auth.get_signup_lang's
            # own docstring), reused for every holding's AI prompt and
            # for the combined email/push below.
            _lang = email_auth.get_signup_lang(email)
            holdings = portfolio_store.get_holdings(email, portfolio)
            briefs = []
            for h in holdings:
                if len(briefs) >= MAX_BRIEFS_PER_USER_PER_NIGHT:
                    log(f"[watchdog] {email}/{portfolio}: hit the "
                        f"{MAX_BRIEFS_PER_USER_PER_NIGHT}/night cap, stopping early")
                    break
                ticker = h["ticker"]
                try:
                    analysis = _analyze_holding(h)
                except Exception as e:
                    summary["errors"] += 1
                    log(f"[watchdog] {email}/{portfolio}/{ticker}: analysis failed: {e}")
                    continue

                health, news = analysis["health"], analysis["news"]
                prev_overall = portfolio_health_engine.record_health_run(
                    email, portfolio, ticker, health.get("overall"),
                    news_risk=(news or {}).get("news_risk_score"),
                )
                if not _is_material(health, news, prev_overall):
                    summary["skipped_no_change"] += 1
                    continue
                sig = _signature(health, news)
                if sig == _get_last_signature(email, portfolio, ticker):
                    summary["skipped_no_change"] += 1
                    continue

                allowed, gate_msg, _tier = ai_gate.check(email, AI_FEATURE)
                if not allowed:
                    summary["gate_blocked"] += 1
                    log(f"[watchdog] {email}/{portfolio}: AI gate blocked "
                        f"({gate_msg}) - stopping this user's run")
                    break

                _system_prompt = _SYSTEM_PROMPT + (_SYSTEM_PROMPT_ES_SUFFIX if _lang == "es" else "")
                result = ai_client.ask(_system_prompt,
                                       _build_prompt(ticker, h, analysis, prev_overall))
                if result["input_tokens"] or result["output_tokens"]:
                    try:
                        ai_gate.record(email, AI_FEATURE, result["model"],
                                       result["input_tokens"], result["output_tokens"],
                                       result["cost_usd"])
                    except Exception:
                        pass
                if not result["ok"]:
                    summary["errors"] += 1
                    log(f"[watchdog] {email}/{portfolio}/{ticker}: AI call failed: "
                        f"{result['error']}")
                    continue

                _record_signature(email, portfolio, ticker, sig)
                briefs.append({"ticker": ticker, "text": result["text"]})
                summary["briefs_sent"] += 1

            if not briefs:
                continue

            sent_anything = False
            _tickers_joined = ", ".join(b["ticker"] for b in briefs)
            if email_configured:
                try:
                    subject = i18n.t("email.watchdog.subject", _lang, tickers=_tickers_joined)
                    _send(email, subject, _email_html(briefs, site, lang=_lang))
                    sent_anything = True
                except Exception as e:
                    summary["errors"] += 1
                    log(f"[watchdog] email to {email}: {e}")
            # Best-effort push alongside the email above, same failure
            # isolation announce_engine.announce_rebuild uses: a bad/
            # expired push subscription must never affect whether the
            # email above sent, and vice versa.
            if push_configured:
                try:
                    title = i18n.t("push.watchdog.title", _lang, tickers=_tickers_joined)
                    push_send.send_to_email(
                        email, title, i18n.t("push.watchdog.body", _lang),
                        url=f"{site}/portfolio",
                    )
                    sent_anything = True
                except Exception as e:
                    log(f"[watchdog] push to {email}: {e}")
            if sent_anything:
                summary["users_notified"] += 1
                log(f"[watchdog] notified {email} ({len(briefs)} holdings)")
        except Exception as e:
            summary["errors"] += 1
            log(f"[watchdog] {email}/{portfolio}: {e}")

    return summary


if __name__ == "__main__":
    run_nightly_watchdog()
