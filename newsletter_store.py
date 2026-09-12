"""
newsletter_store.py

Mega-batch Part 36: the "get the next deep dive by email" list - a
purpose-built newsletter of its own, separate from every other email
mechanism already in this codebase (email_auth's sign-in codes,
digest_engine's per-watchlist weekly re-score, blog_render's older
follow_store.ALL_TICKERS "next research note" box). See the Part 36
deployment report for why this is a NEW table rather than a reuse of one
of those - the short version is the instruction explicitly asked for a
dedicated `newsletter_subscribers` table with its own double opt-in and
its own owner-triggered per-post send, which none of the existing
mechanisms provide together.

REUSES THE EXISTING MAILGUN SETUP - same MAILGUN_API_KEY / MAILGUN_DOMAIN
/ MAILGUN_FROM / MAILGUN_BASE_URL env vars digest_engine.py and
email_auth.py already read. Zero new Railway configuration.

PRIVACY (hard rule, applies to every function in this module):
  - Subscriber emails are stored ONLY to run this list - never shared,
    never returned from any public API surface, never written to a log
    line. Every log call below is a bare event ("[newsletter] confirm
    email sent", "[newsletter] send batch: X ok / Y failed") - none of
    them ever interpolates an email address or an exception object that
    could itself contain one (a Mailgun 4xx/5xx body can echo the
    recipient back). `all_confirmed_emails()` is the one function that
    lets addresses leave this module at all, and it exists ONLY for the
    internal owner-triggered send loop (send_post_notification below) -
    never call it from anything a browser can reach, and never log its
    return value. This is the same discipline email_auth.py documents
    for its own list_signups().
  - No subscriber-list UI exists anywhere on the site - counts only
    (confirmed_count/pending_count), per the instruction's own 36.3 rule.

DOUBLE OPT-IN: subscribe() only ever creates an UNCONFIRMED row and
emails a confirm link (confirm_token, single-use in spirit - see
confirm()'s docstring). A row only starts receiving posts once confirm()
is called with a valid token. Applied uniformly, including a signed-in
visitor's one-click "Subscribe" (their account email was already
verified at sign-in time, but Part 36 is listed as a hard rule with no
carve-out for that case - flagged in the deployment report as an
interpretation call the owner may want to revisit).

UNSUBSCRIBE: unsub_token is a random per-subscriber token, generated at
signup time (not derived from the email, per the instruction), and
unsubscribing DELETES the row outright rather than flipping a flag - an
old, already-used confirm link can then never silently re-subscribe
someone who deliberately left.

ABUSE GUARD: signup emails share the SAME 100/day Mailgun free-tier quota
email_auth.py's sign-in codes depend on (see that module's own docstring)
- an unthrottled public signup form would be a direct way to starve sign-
in of that quota. Mirrors email_auth.py's exact per-address and per-IP
daily caps rather than inventing a new pattern.
"""

import hashlib
import os
import re
import secrets
import sqlite3
import time
from datetime import datetime, timedelta, timezone

import requests


def _data_dir():
    return os.environ.get("RAILWAY_VOLUME_MOUNT_PATH") or os.path.dirname(__file__)


DB_PATH = os.path.join(_data_dir(), "stocksdeepdive.db")

CONFIRM_TTL_DAYS = 7
STALE_UNCONFIRMED_DAYS = 30  # nightly-pruned, see prune_stale_unconfirmed()

MAX_SIGNUP_SENDS_PER_EMAIL_PER_DAY = 5
MAX_SIGNUP_SENDS_PER_IP_PER_DAY = 15
RESEND_MIN_INTERVAL_MINUTES = 5  # don't re-send a confirm email on every rerun/refresh

SEND_BATCH_SIZE = 50
SEND_BATCH_SLEEP_SECONDS = 2

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _conn():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS newsletter_subscribers (
            email TEXT PRIMARY KEY,
            confirmed INTEGER NOT NULL DEFAULT 0,
            confirm_token TEXT,
            confirm_sent_at TEXT,
            confirmed_at TEXT,
            unsub_token TEXT NOT NULL,
            created_at TEXT NOT NULL,
            lang TEXT,
            src TEXT
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS newsletter_ip_sends (
            ip TEXT NOT NULL,
            day TEXT NOT NULL,
            count INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (ip, day)
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS newsletter_sends (
            post_id INTEGER PRIMARY KEY,
            slug TEXT,
            title TEXT,
            sent_at TEXT NOT NULL,
            recipient_count INTEGER NOT NULL DEFAULT 0,
            failed_count INTEGER NOT NULL DEFAULT 0,
            send_count INTEGER NOT NULL DEFAULT 1
        )"""
    )
    return conn


def _now():
    return datetime.now(timezone.utc)


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


def _mailgun_cfg():
    domain = os.environ.get("MAILGUN_DOMAIN", "").strip()
    return {
        "api_key": os.environ.get("MAILGUN_API_KEY", "").strip(),
        "domain": domain,
        # Same MAILGUN_FROM override every other Mailgun sender in this
        # codebase reads first - falls back to a "news@" local part (this
        # module's own identity) only when the owner hasn't set one,
        # exactly like email_auth's "signin@" / digest_engine's "digest@".
        "from": os.environ.get("MAILGUN_FROM", "").strip()
                or (f"StocksDeepDive <news@{domain}>" if domain else ""),
        "base_url": (
            os.environ.get("MAILGUN_BASE_URL", "").strip()
            or os.environ.get("MAILGUN_API_BASE_URL", "").strip().removesuffix("/v3")
            or "https://api.mailgun.net"
        ).rstrip("/"),
        "site": os.environ.get("SITE_BASE_URL", "").strip()
                or "https://stocksdeepdive.com",
    }


def is_configured():
    c = _mailgun_cfg()
    return bool(c["api_key"] and c["domain"])


def valid_email(email):
    return bool(email and _EMAIL_RE.match(email.strip()))


def _send_mailgun(to_email, subject, html_body):
    c = _mailgun_cfg()
    resp = requests.post(
        f"{c['base_url']}/v3/{c['domain']}/messages",
        auth=("api", c["api_key"]),
        data={"from": c["from"], "to": [to_email],
              "subject": subject, "html": html_body},
        timeout=20,
    )
    resp.raise_for_status()


def _ip_sends_today(conn, ip, today):
    row = conn.execute(
        "SELECT count FROM newsletter_ip_sends WHERE ip = ? AND day = ?",
        (ip, today),
    ).fetchone()
    return row[0] if row else 0


def _record_ip_send(conn, ip, today):
    conn.execute(
        """INSERT INTO newsletter_ip_sends (ip, day, count) VALUES (?, ?, 1)
           ON CONFLICT(ip, day) DO UPDATE SET count = count + 1""",
        (ip, today),
    )


# -----------------------------------
# CONFIRM EMAIL (Outlook/Hotmail-safe inline-table HTML - same convention
# as digest_engine._email_html/email_auth._send_email).
# -----------------------------------

def _confirm_email_html(confirm_url, lang):
    import i18n
    return f"""\
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background-color:#f4f7fa;">
<tr><td align="center" style="padding:24px 12px;">
  <table role="presentation" width="440" cellpadding="0" cellspacing="0" border="0" style="background-color:#ffffff;border:1px solid #e2e8f0;">
    <tr><td style="padding:24px 28px;font-family:Arial,Helvetica,sans-serif;">
      <span style="font-size:20px;font-weight:bold;color:#0f172a;">Stocks</span><span style="font-size:20px;font-weight:bold;color:#0d9488;">DeepDive</span>
      <div style="font-size:15px;color:#334155;padding-top:14px;font-weight:bold;">{i18n.t("email.newsletter_confirm.heading", lang)}</div>
      <div style="font-size:13px;color:#64748b;padding-top:8px;">{i18n.t("email.newsletter_confirm.body", lang)}</div>
      <div style="padding-top:18px;">
        <a href="{confirm_url}" style="display:inline-block;background-color:#0d9488;color:#ffffff;
          text-decoration:none;padding:11px 22px;font-family:Arial,Helvetica,sans-serif;
          font-size:14px;font-weight:bold;">{i18n.t("email.newsletter_confirm.button", lang)}</a>
      </div>
      <div style="font-size:11px;color:#94a3b8;padding-top:18px;">{i18n.t("email.newsletter_confirm.ignore", lang)}</div>
    </td></tr>
  </table>
</td></tr>
</table>
"""


def _send_confirm_email(email, token, lang):
    import i18n
    site = _mailgun_cfg()["site"]
    confirm_url = f"{site}/newsletter/confirm?token={token}"
    _send_mailgun(
        email,
        i18n.t("email.newsletter_confirm.subject", lang),
        _confirm_email_html(confirm_url, lang),
    )


def subscribe(email, lang=None, src=None, client_ip=None):
    """Entry point for BOTH the signed-out (typed) and signed-in (one-
    click, account email) flows - the caller decides which email to pass,
    this function treats them identically (see module docstring on why
    double opt-in applies to both). Returns (ok, message) - message is
    already localized via i18n.t(), same 2-tuple convention as
    email_auth.send_code()."""
    import i18n
    lang = (lang or "en").strip().lower()
    if lang not in ("en", "es"):
        lang = "en"
    email = (email or "").strip().lower()
    if not valid_email(email):
        return False, i18n.t("email_signup.invalid_email", lang)
    if not is_configured():
        return False, i18n.t("email_signup.not_configured", lang)

    today = _now().strftime("%Y-%m-%d")
    client_ip = (client_ip or "").strip() or None
    with _conn() as conn:
        if client_ip and _ip_sends_today(conn, client_ip, today) >= MAX_SIGNUP_SENDS_PER_IP_PER_DAY:
            return False, i18n.t("email_signup.too_many", lang)

        row = conn.execute(
            "SELECT confirmed, confirm_sent_at FROM newsletter_subscribers WHERE email = ?",
            (email,),
        ).fetchone()

        if row and row[0]:
            # Idempotent both ways (Verify 2): already confirmed, nothing
            # to send, no error.
            return True, i18n.t("email_signup.already_confirmed", lang)

        if row and row[1]:
            # String comparison, not datetime subtraction - _iso() stores
            # naive (no-tzinfo) UTC timestamps, same convention as every
            # other store module in this codebase (email_auth.py's
            # expires_at/last_send_date checks, etc.), specifically to
            # avoid the naive-vs-aware TypeError parsing one back into a
            # datetime would risk.
            resend_cutoff = _iso(_now() - timedelta(minutes=RESEND_MIN_INTERVAL_MINUTES))
            if row[1] > resend_cutoff:
                # Same click landing twice (double-submit, a Streamlit
                # rerun) - don't burn a second Mailgun send or reset the
                # token's expiry for a request that isn't a genuine retry.
                return True, i18n.t("email_signup.pending_resent", lang)

        src = (src or "").strip() or None
        token = secrets.token_urlsafe(32)
        now_iso = _iso(_now())
        if row:
            conn.execute(
                """UPDATE newsletter_subscribers
                   SET confirm_token = ?, confirm_sent_at = ?, lang = ?, src = COALESCE(src, ?)
                   WHERE email = ?""",
                (token, now_iso, lang, src, email),
            )
        else:
            conn.execute(
                """INSERT INTO newsletter_subscribers
                   (email, confirmed, confirm_token, confirm_sent_at,
                    confirmed_at, unsub_token, created_at, lang, src)
                   VALUES (?, 0, ?, ?, NULL, ?, ?, ?, ?)""",
                (email, token, now_iso, secrets.token_urlsafe(32), now_iso, lang, src),
            )
        if client_ip:
            _record_ip_send(conn, client_ip, today)

    try:
        _send_confirm_email(email, token, lang)
    except Exception:
        return False, i18n.t("email_signup.send_failed", lang)
    return True, i18n.t("email_signup.sent", lang)


def confirm(token):
    """Click-through from the confirm email. Returns (ok, message, lang)
    - lang is the subscriber's own recorded language, for the landing
    page server.py renders (never a query param, since the emailed link
    carries no lang - it's looked up server-side from the row itself).
    Idempotent: re-clicking an already-confirmed link is a friendly
    success, not an error. An unknown/garbled token, or one whose
    CONFIRM_TTL_DAYS window has passed without ever being confirmed, is
    reported as invalid/expired rather than silently doing nothing."""
    import i18n
    token = (token or "").strip()
    if not token:
        return False, i18n.t("newsletter_confirm_page.invalid", "en"), "en"
    with _conn() as conn:
        row = conn.execute(
            """SELECT email, confirmed, confirm_sent_at, lang
               FROM newsletter_subscribers WHERE confirm_token = ?""",
            (token,),
        ).fetchone()
        if not row:
            return False, i18n.t("newsletter_confirm_page.invalid", "en"), "en"
        email, confirmed, confirm_sent_at, lang = row
        lang = lang if lang in ("en", "es") else "en"
        if confirmed:
            return True, i18n.t("newsletter_confirm_page.body", lang), lang
        if confirm_sent_at:
            # String comparison against a cutoff string - see subscribe()'s
            # own comment on why (naive-vs-aware datetime subtraction is
            # not safe against _iso()'s stored format).
            expiry_cutoff = _iso(_now() - timedelta(days=CONFIRM_TTL_DAYS))
            if confirm_sent_at < expiry_cutoff:
                return False, i18n.t("newsletter_confirm_page.expired", lang), lang
        conn.execute(
            """UPDATE newsletter_subscribers
               SET confirmed = 1, confirmed_at = ? WHERE email = ?""",
            (_iso(_now()), email),
        )
    return True, i18n.t("newsletter_confirm_page.body", lang), lang


def unsubscribe(token):
    """One click, no sign-in, immediate (Verify 3) - deletes the row
    outright rather than flipping a flag, so a stale confirm link can
    never silently re-subscribe someone who left. Returns (ok, message,
    lang)."""
    import i18n
    token = (token or "").strip()
    if not token:
        return False, i18n.t("newsletter_unsub_page.invalid", "en"), "en"
    with _conn() as conn:
        row = conn.execute(
            "SELECT lang FROM newsletter_subscribers WHERE unsub_token = ?",
            (token,),
        ).fetchone()
        if not row:
            return False, i18n.t("newsletter_unsub_page.invalid", "en"), "en"
        lang = row[0] if row[0] in ("en", "es") else "en"
        conn.execute("DELETE FROM newsletter_subscribers WHERE unsub_token = ?", (token,))
    return True, i18n.t("newsletter_unsub_page.body", lang), lang


def subscription_status(email):
    """"confirmed" / "pending" / None - for the signed-in one-click box to
    show the right state without a second confirm-email being fired on
    every page load."""
    email = (email or "").strip().lower()
    if not email:
        return None
    with _conn() as conn:
        row = conn.execute(
            "SELECT confirmed FROM newsletter_subscribers WHERE email = ?",
            (email,),
        ).fetchone()
    if not row:
        return None
    return "confirmed" if row[0] else "pending"


def confirmed_count():
    with _conn() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM newsletter_subscribers WHERE confirmed = 1"
        ).fetchone()[0]


def pending_count():
    with _conn() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM newsletter_subscribers WHERE confirmed = 0"
        ).fetchone()[0]


def all_confirmed_emails():
    """IDENTITY-BEARING - see the module docstring's privacy section.
    Internal use ONLY by send_post_notification() below. Never call this
    from a route/page a browser can reach, and never log the result."""
    with _conn() as conn:
        rows = conn.execute(
            """SELECT email, unsub_token, lang FROM newsletter_subscribers
               WHERE confirmed = 1"""
        ).fetchall()
    return [{"email": r[0], "unsub_token": r[1], "lang": r[2] or "en"} for r in rows]


# -----------------------------------
# OWNER-TRIGGERED SEND (36.2) - never called from save/publish, only from
# the Blog admin "Send to subscribers" button.
# -----------------------------------

def _notify_email_html(title, excerpt, link, unsub_url, email, lang):
    import html as _html_mod
    import i18n
    e_title = title
    email = _html_mod.escape(email)
    return f"""\
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background-color:#f4f7fa;">
<tr><td align="center" style="padding:24px 12px;">
  <table role="presentation" width="560" cellpadding="0" cellspacing="0" border="0" style="background-color:#ffffff;border:1px solid #e2e8f0;">
    <tr><td style="padding:24px 28px 6px 28px;font-family:Arial,Helvetica,sans-serif;">
      <span style="font-size:22px;font-weight:bold;color:#0f172a;">Stocks</span><span style="font-size:22px;font-weight:bold;color:#0d9488;">DeepDive</span>
      <div style="font-size:12px;color:#0d9488;padding-top:10px;text-transform:uppercase;letter-spacing:0.5px;font-weight:bold;">{i18n.t("email.newsletter_notify.heading", lang)}</div>
      <div style="font-size:19px;color:#0f172a;padding-top:6px;font-weight:bold;">{e_title}</div>
      <div style="font-size:14px;color:#334155;padding-top:10px;line-height:1.5;">{excerpt}</div>
      <div style="padding-top:16px;">
        <a href="{link}" style="display:inline-block;background-color:#0d9488;color:#ffffff;
          text-decoration:none;padding:10px 20px;font-family:Arial,Helvetica,sans-serif;
          font-size:14px;font-weight:bold;">{i18n.t("email.newsletter_notify.cta", lang)}</a>
      </div>
    </td></tr>
    <tr><td style="padding:16px 28px 24px 28px;font-family:Arial,Helvetica,sans-serif;font-size:11px;color:#94a3b8;line-height:1.6;">
      {i18n.t("email.newsletter_notify.footer", lang, email=email)}
      <a href="{unsub_url}" style="color:#94a3b8;">{i18n.t("email.newsletter_notify.unsubscribe", lang)}</a>
    </td></tr>
  </table>
</td></tr>
</table>
"""


def send_post_notification(post, log=print):
    """Owner-triggered ONLY (36.2's hard rule) - the Blog admin "Send to
    subscribers" button is the one and only caller; never invoked from
    save/publish. Sends `post`'s title + excerpt + a src=newsletter link
    to every CONFIRMED subscriber, throttled client-side in batches of
    SEND_BATCH_SIZE with a short sleep between batches (this codebase's
    every other Mailgun sender - digest_engine, email_auth - already
    makes one API call per recipient rather than using Mailgun's own
    recipient-variables batch endpoint, so batching here means the same
    thing: group the per-recipient calls and pause between groups, not a
    single multi-recipient API call - flagged as an interpretation call
    in the deployment report since "batches" could plausibly have meant
    either). Per-recipient try/except, same failure model as digest_
    engine.run_weekly_digest - one bounced address never stops the rest.
    Returns {'sent', 'failed', 'total'}. Never logs an email address."""
    summary = {"sent": 0, "failed": 0, "total": 0}
    if not is_configured():
        log("[newsletter] Mailgun not configured - skipping send")
        return summary

    subs = all_confirmed_emails()
    summary["total"] = len(subs)
    if not subs:
        log("[newsletter] send requested but there are no confirmed subscribers")
        return summary

    site = _mailgun_cfg()["site"]
    slug = post.get("slug") or ""
    link = f"{site}/blog/{slug}?src=newsletter"
    title = post.get("title") or ""

    # blog_render.post_description() is the same excerpt logic the blog's
    # own <meta description> already uses (summary, else a Markdown-
    # stripped ~155-char lead) - reused rather than re-derived so the
    # email and the page never describe a post two different ways.
    try:
        import blog_render
        excerpt = blog_render.post_description(post)
    except Exception:
        excerpt = (post.get("summary") or "").strip()

    import html as _html_mod
    e_title = _html_mod.escape(title)
    e_excerpt = _html_mod.escape(excerpt)

    for i in range(0, len(subs), SEND_BATCH_SIZE):
        batch = subs[i:i + SEND_BATCH_SIZE]
        for sub in batch:
            try:
                unsub_url = f"{site}/newsletter/unsubscribe?token={sub['unsub_token']}"
                _send_mailgun(
                    sub["email"],
                    _subject_for(title, sub["lang"]),
                    _notify_email_html(e_title, e_excerpt, link, unsub_url,
                                       sub["email"], sub["lang"]),
                )
                summary["sent"] += 1
            except Exception:
                summary["failed"] += 1
        if i + SEND_BATCH_SIZE < len(subs):
            time.sleep(SEND_BATCH_SLEEP_SECONDS)

    log(f"[newsletter] post {post.get('id')}: sent {summary['sent']} / "
        f"failed {summary['failed']} of {summary['total']} confirmed subscriber(s)")
    _record_send(post.get("id"), slug, title, summary["sent"], summary["failed"])
    return summary


def _subject_for(title, lang):
    import i18n
    return i18n.t("email.newsletter_notify.subject", lang or "en", title=title)


def _record_send(post_id, slug, title, sent_count, failed_count):
    if not post_id:
        return
    with _conn() as conn:
        conn.execute(
            """INSERT INTO newsletter_sends
               (post_id, slug, title, sent_at, recipient_count, failed_count, send_count)
               VALUES (?, ?, ?, ?, ?, ?, 1)
               ON CONFLICT(post_id) DO UPDATE SET
                 slug = excluded.slug,
                 title = excluded.title,
                 sent_at = excluded.sent_at,
                 recipient_count = excluded.recipient_count,
                 failed_count = excluded.failed_count,
                 send_count = send_count + 1""",
            (post_id, slug, title, _iso(_now()), sent_count, failed_count),
        )


def send_status(post_id):
    """The post's own send row, or None if it's never been sent - the
    Blog admin uses this to decide "Send to subscribers" vs. "sent to N
    subscribers on <date>" (a deliberate re-send still needs its own
    confirm step, per 36.2 - handled by the admin page, not here)."""
    if not post_id:
        return None
    with _conn() as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM newsletter_sends WHERE post_id = ?", (post_id,)
        ).fetchone()
    return dict(row) if row else None


def last_send_summary():
    """Most recent send across every post, for the Admin Dashboard (36.3)
    "last send summary" line."""
    with _conn() as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM newsletter_sends ORDER BY sent_at DESC LIMIT 1"
        ).fetchone()
    return dict(row) if row else None


def prune_stale_unconfirmed(log=print):
    """Nightly housekeeping (wired into scheduler_engine._run_volume_check,
    the same slot admin_metrics_store's own retention prunes run in):
    deletes rows that were never confirmed and are older than
    STALE_UNCONFIRMED_DAYS. A generous window (30 days) - the read-side
    CONFIRM_TTL_DAYS=7 already refuses an expired link's confirm attempt
    well before this would ever touch it; this just keeps the table from
    accumulating dead, mistyped or abandoned addresses indefinitely. Also
    prunes newsletter_ip_sends rows older than 2 days, mirroring email_
    auth.cleanup()'s identical ip-sends prune."""
    cutoff = _iso(_now() - timedelta(days=STALE_UNCONFIRMED_DAYS))
    ip_cutoff = (_now() - timedelta(days=2)).strftime("%Y-%m-%d")
    with _conn() as conn:
        cur = conn.execute(
            "DELETE FROM newsletter_subscribers WHERE confirmed = 0 AND created_at < ?",
            (cutoff,),
        )
        n = max(cur.rowcount, 0)
        conn.execute("DELETE FROM newsletter_ip_sends WHERE day < ?", (ip_cutoff,))
    if n:
        log(f"[newsletter] pruned {n} stale unconfirmed signup(s)")
