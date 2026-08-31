"""
comment_triage_store.py

AI-readiness roadmap Phase 10: "AI-assisted comment moderation (spam/abuse
triage for the admin queue)". A tiny cache of one AI-computed triage label
per PENDING blog comment, read by app.py's page_blog_admin() comment queue
(see that page's own "comment moderation" section) to help the owner scan
a list of pending comments faster - a coloured LABEL and a one-line reason
next to each comment, nothing more.

NOT a moderation decision. The AI never approves, rejects, or hides a
comment - blog_comments_store.set_status() (called only from the admin's
own Approve/Reject buttons) remains the ONLY path a comment's status ever
changes through, exactly as before this phase. This store only ever
ADDS a label for the admin to read; it never writes to blog_comments_store
itself. Same human-in-the-loop-by-construction discipline Phase 7's
research-note drafter and Phase 8/9's AI content already follow.

Cached forever per comment id (not TTL'd, unlike explain_cache_store.py's
per-ticker+metric cache) - a submitted comment's text never changes, so
once triaged there is nothing to ever go stale; the cache only needs
invalidating if the admin explicitly asks for a re-check (see
clear() below, used if a triage call ever needs to be re-run by hand).

Same SQLite file / volume-resolution rule as blog_comments_store.py
(reuses that module's own DB_PATH rather than resolving it separately)."""

import sqlite3
from datetime import datetime, timezone

import blog_comments_store

DB_PATH = blog_comments_store.DB_PATH

LABEL_OK = "OK"
LABEL_SPAM = "SPAM"
LABEL_ABUSE = "ABUSE"
LABEL_UNKNOWN = "UNKNOWN"
VALID_LABELS = (LABEL_OK, LABEL_SPAM, LABEL_ABUSE, LABEL_UNKNOWN)


def _conn():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS comment_triage (
            comment_id INTEGER PRIMARY KEY,
            label TEXT NOT NULL,
            reason TEXT,
            model TEXT,
            created_at TEXT NOT NULL
        )"""
    )
    return conn


def get(comment_id):
    """{'label', 'reason', 'model'} for a previously-triaged comment, or
    None if it hasn't been triaged yet."""
    if not comment_id:
        return None
    with _conn() as conn:
        row = conn.execute(
            "SELECT label, reason, model FROM comment_triage WHERE comment_id = ?",
            (comment_id,),
        ).fetchone()
    if not row:
        return None
    return {"label": row[0], "reason": row[1], "model": row[2]}


def set(comment_id, label, reason=None, model=None):
    if not comment_id:
        return
    if label not in VALID_LABELS:
        label = LABEL_UNKNOWN
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as conn:
        conn.execute(
            """INSERT INTO comment_triage (comment_id, label, reason, model, created_at)
                 VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(comment_id) DO UPDATE SET
                 label = excluded.label, reason = excluded.reason,
                 model = excluded.model, created_at = excluded.created_at""",
            (comment_id, label, reason, model, now),
        )


def clear(comment_id):
    """Removes a cached triage so the next admin-page render re-triages
    it from scratch - not wired to any button today, but here so a stuck
    or wrong-looking label can be cleared by hand (e.g. from a Python
    shell) without needing a schema change later."""
    if not comment_id:
        return
    with _conn() as conn:
        conn.execute("DELETE FROM comment_triage WHERE comment_id = ?", (comment_id,))
