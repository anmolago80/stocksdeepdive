"""
email_auth.py

Passwordless "sign in with email" + aggregate sign-up stats.

HOW IT WORKS (no passwords ever stored):
  1. Visitor types their email -> a 6-digit code is emailed via Mailgun
     (same MAILGUN_API_KEY / MAILGUN_DOMAIN the digest and feedback
     engines already use - zero new configuration).
  2. They type the code -> a long random session token is minted, its
     hash stored server-side, and the raw token goes into a browser
     cookie so they stay signed in across visits (90 days).
  3. paywall_engine treats a valid email session exactly like a Google
     sign-in: same watchlists, same digest, same gates.

STORAGE: the same SQLite file (and volume-resolution rule) as
watchlist_store - codes and sessions are hashed (sha256), so a leaked DB
never yields a usable code or cookie value.

SIGN-UP REGISTRY: every first sign-in (email OR Google) upserts a row in
`signups`. The admin panel only ever reads AGGREGATE COUNTS from it
(total / last 7 days / last 30 days, split by method) - per Andrew's
choice, no identities are displayed anywhere.

ABUSE LIMITS: max 5 code emails per address per day, codes expire after
15 minutes, and 5 wrong attempts burn the code.
"""

import hashlib
import os
import re
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone

import requests


def _data_dir():
    return os.environ.get("RAILWAY_VOLUME_MOUNT_PATH") or os.path.dirname(__file__)


DB_PATH = os.path.join(_data_dir(), "stocksdeepdive.db")

CODE_TTL_MINUTES = 15
MAX_ATTEMPTS = 5
MAX_SENDS_PER_DAY = 5
SESSION_TTL_DAYS = 90

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _conn():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS auth_codes (
            email TEXT PRIMARY KEY,
            code_hash TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            sends_today INTEGER NOT NULL DEFAULT 0,
            last_send_date TEXT NOT NULL
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS auth_sessions (
            token_hash TEXT PRIMARY KEY,
            email TEXT NOT NULL,
            created_at TEXT NOT NULL,
            last_seen TEXT NOT NULL
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS signups (
            email TEXT PRIMARY KEY,
            method TEXT NOT NULL,
            first_seen TEXT NOT NULL,
            last_seen TEXT NOT NULL,
            signin_count INTEGER NOT NULL DEFAULT 1,
            src TEXT
        )"""
    )
    # ALTER TABLE guarded by try/except so a DB created before the `src`
    # column existed still works (CREATE TABLE above only applies to a
    # brand-new file) - belt and braces per the CREATE TABLE already
    # having the column too.
    try:
        conn.execute("ALTER TABLE signups ADD COLUMN src TEXT")
    except sqlite3.OperationalError:
        pass
    return conn


def _now():
    return datetime.now(timezone.utc)


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


def _hash(value):
    return hashlib.sha256(value.encode()).hexdigest()


def _mailgun_cfg():
    domain = os.environ.get("MAILGUN_DOMAIN", "").strip()
    return {
        "api_key": os.environ.get("MAILGUN_API_KEY", "").strip(),
        "domain": domain,
        "from": os.environ.get("MAILGUN_FROM", "").strip()
                or (f"StocksDeepDive <signin@{domain}>" if domain else ""),
        "base_url": (
            os.environ.get("MAILGUN_BASE_URL", "").strip()
            or os.environ.get("MAILGUN_API_BASE_URL", "").strip().removesuffix("/v3")
            or "https://api.mailgun.net"
        ).rstrip("/"),
    }


def is_configured():
    c = _mailgun_cfg()
    return bool(c["api_key"] and c["domain"])


def valid_email(email):
    return bool(email and _EMAIL_RE.match(email.strip()))


def _send_email(to_email, code):
    c = _mailgun_cfg()
    html = f"""\
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background-color:#f4f7fa;">
<tr><td align="center" style="padding:24px 12px;">
  <table role="presentation" width="440" cellpadding="0" cellspacing="0" border="0" style="background-color:#ffffff;border:1px solid #e2e8f0;">
    <tr><td style="padding:24px 28px;font-family:Arial,Helvetica,sans-serif;">
      <span style="font-size:20px;font-weight:bold;color:#0f172a;">Stocks</span><span style="font-size:20px;font-weight:bold;color:#0d9488;">DeepDive</span>
      <div style="font-size:14px;color:#334155;padding-top:14px;">Your sign-in code:</div>
      <div style="font-size:32px;font-weight:bold;letter-spacing:8px;color:#0f172a;padding:14px 0;">{code}</div>
      <div style="font-size:12px;color:#64748b;">This code expires in {CODE_TTL_MINUTES} minutes.
      If you didn't request it, you can ignore this email - nothing happens without the code.</div>
    </td></tr>
  </table>
</td></tr>
</table>
"""
    resp = requests.post(
        f"{c['base_url']}/v3/{c['domain']}/messages",
        auth=("api", c["api_key"]),
        data={"from": c["from"], "to": [to_email],
              "subject": f"Your StocksDeepDive sign-in code: {code}",
              "html": html},
        timeout=15,
    )
    resp.raise_for_status()


def send_code(email):
    """Email a fresh 6-digit code. Returns (ok, user_message)."""
    email = (email or "").strip().lower()
    if not valid_email(email):
        return False, "That doesn't look like a valid email address."
    if not is_configured():
        return False, "Email sign-in isn't available right now - try Google."

    today = _now().strftime("%Y-%m-%d")
    with _conn() as conn:
        row = conn.execute(
            "SELECT sends_today, last_send_date FROM auth_codes WHERE email = ?",
            (email,),
        ).fetchone()
        sends_today = row[0] if row and row[1] == today else 0
        if sends_today >= MAX_SENDS_PER_DAY:
            return False, "Too many codes requested today - please try again tomorrow."

        code = f"{secrets.randbelow(1_000_000):06d}"
        expires = _iso(_now() + timedelta(minutes=CODE_TTL_MINUTES))
        conn.execute(
            """INSERT INTO auth_codes (email, code_hash, expires_at, attempts,
                                       sends_today, last_send_date)
               VALUES (?, ?, ?, 0, ?, ?)
               ON CONFLICT(email) DO UPDATE SET
                 code_hash = excluded.code_hash,
                 expires_at = excluded.expires_at,
                 attempts = 0,
                 sends_today = excluded.sends_today,
                 last_send_date = excluded.last_send_date""",
            (email, _hash(f"{email}:{code}"), expires, sends_today + 1, today),
        )
    try:
        _send_email(email, code)
    except Exception:
        return False, "Couldn't send the email right now - please try again."
    return True, f"Code sent to {email} - check your inbox (and spam folder)."


def verify_code(email, code, src=None):
    """Check a code. Returns (session_token, user_message) - token is None
    on failure. On success the code is consumed and a session created.
    `src` (the caller's first_src, e.g. an article slug) is recorded on the
    sign-up row when this is a first sign-in - callers pass
    st.session_state.get("first_src"); this module itself never imports
    streamlit."""
    email = (email or "").strip().lower()
    code = (code or "").strip()
    if not valid_email(email) or not code:
        return None, "Enter the 6-digit code from the email."
    with _conn() as conn:
        row = conn.execute(
            "SELECT code_hash, expires_at, attempts FROM auth_codes WHERE email = ?",
            (email,),
        ).fetchone()
        if not row:
            return None, "No code on record - request a new one."
        code_hash, expires_at, attempts = row
        if attempts >= MAX_ATTEMPTS:
            return None, "Too many wrong attempts - request a new code."
        if _iso(_now()) > expires_at:
            return None, "That code has expired - request a new one."
        if _hash(f"{email}:{code}") != code_hash:
            conn.execute(
                "UPDATE auth_codes SET attempts = attempts + 1 WHERE email = ?",
                (email,),
            )
            return None, "Wrong code - check the email and try again."
        # Success: consume the code, mint a session.
        conn.execute("DELETE FROM auth_codes WHERE email = ?", (email,))
        token = secrets.token_hex(32)
        now = _iso(_now())
        conn.execute(
            "INSERT INTO auth_sessions (token_hash, email, created_at, last_seen) "
            "VALUES (?, ?, ?, ?)",
            (_hash(token), email, now, now),
        )
    record_signup(email, "email", src=src)
    return token, "Signed in."


def session_email(token):
    """The email behind a session token, or None if unknown/expired."""
    if not token:
        return None
    cutoff = _iso(_now() - timedelta(days=SESSION_TTL_DAYS))
    with _conn() as conn:
        row = conn.execute(
            "SELECT email, created_at FROM auth_sessions WHERE token_hash = ?",
            (_hash(token),),
        ).fetchone()
        if not row:
            return None
        email, created_at = row
        if created_at < cutoff:
            conn.execute("DELETE FROM auth_sessions WHERE token_hash = ?",
                         (_hash(token),))
            return None
        conn.execute(
            "UPDATE auth_sessions SET last_seen = ? WHERE token_hash = ?",
            (_iso(_now()), _hash(token)),
        )
    return email


def revoke(token):
    if not token:
        return
    with _conn() as conn:
        conn.execute("DELETE FROM auth_sessions WHERE token_hash = ?",
                     (_hash(token),))


def cleanup():
    """Housekeeping: delete `auth_codes` rows whose `expires_at` is more
    than a day old (a code that's been dead a full day is never going to be
    verified, used or not) and `auth_sessions` rows older than
    SESSION_TTL_DAYS (mirrors the expiry session_email() already enforces
    on read - this just removes the dead rows from disk instead of leaving
    them for every future query to filter past). Called once per process
    by paywall_engine.restore_email_session via its @st.cache_resource
    once-per-boot guard - callers wrap this in try/except, housekeeping
    must never break sign-in."""
    codes_cutoff = _iso(_now() - timedelta(days=1))
    sessions_cutoff = _iso(_now() - timedelta(days=SESSION_TTL_DAYS))
    with _conn() as conn:
        conn.execute("DELETE FROM auth_codes WHERE expires_at < ?", (codes_cutoff,))
        conn.execute("DELETE FROM auth_sessions WHERE created_at < ?", (sessions_cutoff,))


def record_signup(email, method, src=None):
    """Upsert the sign-up registry (kept for aggregate counts only). `src`
    is the visitor's first_src (e.g. an article slug) if one was captured
    this session - stored once at first sign-up and never overwritten by a
    later sign-in, so it reflects what actually brought them here."""
    email = (email or "").strip().lower()
    if not email:
        return
    now = _iso(_now())
    src = (src or "").strip() or None
    with _conn() as conn:
        conn.execute(
            """INSERT INTO signups (email, method, first_seen, last_seen, signin_count, src)
               VALUES (?, ?, ?, ?, 1, ?)
               ON CONFLICT(email) DO UPDATE SET
                 last_seen = excluded.last_seen,
                 signin_count = signin_count + 1""",
            (email, method, now, now, src),
        )


def signup_stats():
    """AGGREGATE counts only - no identities leave this module.
    Returns {'total', 'email', 'google', 'last_7_days', 'last_30_days',
             'active_7_days'}."""
    d7 = _iso(_now() - timedelta(days=7))
    d30 = _iso(_now() - timedelta(days=30))
    with _conn() as conn:
        total = conn.execute("SELECT COUNT(*) FROM signups").fetchone()[0]
        by_email = conn.execute(
            "SELECT COUNT(*) FROM signups WHERE method = 'email'").fetchone()[0]
        by_google = conn.execute(
            "SELECT COUNT(*) FROM signups WHERE method = 'google'").fetchone()[0]
        last7 = conn.execute(
            "SELECT COUNT(*) FROM signups WHERE first_seen >= ?", (d7,)).fetchone()[0]
        last30 = conn.execute(
            "SELECT COUNT(*) FROM signups WHERE first_seen >= ?", (d30,)).fetchone()[0]
        active7 = conn.execute(
            "SELECT COUNT(*) FROM signups WHERE last_seen >= ?", (d7,)).fetchone()[0]
    return {"total": total, "email": by_email, "google": by_google,
            "last_7_days": last7, "last_30_days": last30,
            "active_7_days": active7}


def signup_counts_by_src():
    """AGGREGATE sign-up counts grouped by first_src, e.g. {'article-1': 4}
    - used by the admin Stats popover to line up against
    metrics_store.stats()['by_src']. Rows with no recorded src are
    excluded. No identities leave this module."""
    with _conn() as conn:
        rows = conn.execute(
            """SELECT src, COUNT(*) FROM signups
               WHERE src IS NOT NULL AND src != '' GROUP BY src"""
        ).fetchall()
    return {src: count for src, count in rows}
