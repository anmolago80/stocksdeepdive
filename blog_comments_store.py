"""
blog_comments_store.py

Moderated comments on blog posts (see blog_store.py's own docstring for
why the blog is served outside Streamlit, as real server-rendered HTML).
Same SQLite file blog_store.py uses, same volume-resolution rule -
comments live in the site's one persistent DB alongside posts, so they
survive redeploys too.

Every comment is stored as 'pending' and never shown publicly until an
admin approves it from the blog-admin page (page_blog_admin in app.py).
Spam guards run inside add_comment() itself, not just at the call site,
so any future caller gets them for free: a honeypot field, a per-IP
daily cap, length caps, and an auto-reject when a comment carries more
than two URL-looking substrings.

Callers never touch SQL directly - same convention as blog_store.py.
"""

import contextlib
import hashlib
import re
import sqlite3
from datetime import datetime, timedelta, timezone

import blog_store  # reuse the same DB file / volume-resolution logic

DB_PATH = blog_store.DB_PATH

STATUS_PENDING = "pending"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"

MAX_BODY_CHARS = 2000
MAX_NAME_CHARS = 80
MAX_PENDING_PER_IP_PER_DAY = 5
MAX_URLS_BEFORE_AUTOREJECT = 2

_URL_RE = re.compile(r"https?://|www\.", re.IGNORECASE)


def _now():
    return datetime.now(timezone.utc).isoformat()


def hash_ip(ip):
    """One-way hash - the raw IP is never stored. Salted with a fixed,
    non-secret string; good enough for a same-IP rate limit, not meant
    to survive a targeted deanonymization attempt."""
    return hashlib.sha256(f"sdd-comment-salt:{ip or ''}".encode()).hexdigest()[:32]


@contextlib.contextmanager
def _conn():
    """Same `with _conn() as conn:` commit-or-rollback-and-close pattern
    as blog_store._conn() - see that function's docstring for why this
    is a @contextmanager rather than a bare Connection."""
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS blog_comments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            post_slug TEXT NOT NULL,
            author_name TEXT NOT NULL DEFAULT '',
            body TEXT NOT NULL,
            created_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            ip_hash TEXT NOT NULL DEFAULT ''
        )"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_blog_comments_slug_status "
        "ON blog_comments (post_slug, status)"
    )
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _pending_count_today(ip_hash):
    since = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    with _conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM blog_comments WHERE ip_hash = ? AND created_at >= ?",
            (ip_hash, since),
        ).fetchone()
    return row[0] if row else 0


def add_comment(post_slug, author_name, body, ip_hash, honeypot=""):
    """Validates and stores one comment as pending (or auto-rejects it as
    spam). Returns {"ok": bool, "status": "pending"|"rejected"|"error",
    "reason": str|None} - never raises for a bad/spammy submission, only
    for a genuine storage failure.

    Spam guards, in order:
      1. Honeypot: any value in the hidden field means a bot filled every
         input on the form - silently stored as rejected and reported
         back as ok=True (same "thanks, pending review" response a real
         visitor gets), so a bot can't tell it was caught.
      2. Empty body after stripping.
      3. Length caps on name/body.
      4. Per-IP daily cap (MAX_PENDING_PER_IP_PER_DAY submissions in any
         rolling 24h) - refused outright, nothing stored.
      5. More than MAX_URLS_BEFORE_AUTOREJECT URL-looking substrings -
         auto-rejected (still stored, for the admin queue's visibility,
         but never shown publicly) rather than queued pending - link
         spam is the single most common shape of comment spam.
    """
    body = (body or "").strip()
    author_name = (author_name or "").strip()

    if honeypot:
        with _conn() as conn:
            conn.execute(
                """INSERT INTO blog_comments
                     (post_slug, author_name, body, created_at, status, ip_hash)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (post_slug, author_name, body or "(honeypot)", _now(),
                 STATUS_REJECTED, ip_hash),
            )
        return {"ok": True, "status": STATUS_REJECTED, "reason": None}

    if not body:
        return {"ok": False, "status": "error", "reason": "Comment can't be empty."}
    if len(body) > MAX_BODY_CHARS:
        return {"ok": False, "status": "error",
                "reason": f"Comment is too long (max {MAX_BODY_CHARS} characters)."}
    if len(author_name) > MAX_NAME_CHARS:
        return {"ok": False, "status": "error",
                "reason": f"Name is too long (max {MAX_NAME_CHARS} characters)."}
    if _pending_count_today(ip_hash) >= MAX_PENDING_PER_IP_PER_DAY:
        return {"ok": False, "status": "error",
                "reason": "Too many comments submitted today - try again tomorrow."}

    url_count = len(_URL_RE.findall(body))
    status = STATUS_REJECTED if url_count > MAX_URLS_BEFORE_AUTOREJECT else STATUS_PENDING

    with _conn() as conn:
        conn.execute(
            """INSERT INTO blog_comments
                 (post_slug, author_name, body, created_at, status, ip_hash)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (post_slug, author_name, body, _now(), status, ip_hash),
        )
    return {"ok": True, "status": status, "reason": None}


def approved_for(slug):
    """Approved comments for one post, oldest first - a discussion reads
    top-to-bottom in the order it happened."""
    with _conn() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM blog_comments WHERE post_slug = ? AND status = ? "
            "ORDER BY created_at ASC",
            (slug, STATUS_APPROVED),
        ).fetchall()
    return [dict(r) for r in rows]


def pending_all():
    """Every pending comment, newest first - the admin queue. Carries
    only post_slug; the admin page resolves slug -> title itself via
    blog_store.get_post(slug, include_drafts=True)."""
    with _conn() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM blog_comments WHERE status = ? ORDER BY created_at DESC",
            (STATUS_PENDING,),
        ).fetchall()
    return [dict(r) for r in rows]


def pending_count():
    with _conn() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM blog_comments WHERE status = ?", (STATUS_PENDING,)
        ).fetchone()[0]


def set_status(comment_id, status):
    if status not in (STATUS_PENDING, STATUS_APPROVED, STATUS_REJECTED):
        raise ValueError(f"invalid status: {status!r}")
    with _conn() as conn:
        conn.execute(
            "UPDATE blog_comments SET status = ? WHERE id = ?", (status, comment_id)
        )
