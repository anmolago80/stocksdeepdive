"""
share_link_store.py

TASK 60 (Director-directed/owner-approved, 15 Nov 2026): owner-only
share links. A share link opens exactly ONE gated page/item for a
signed-out visitor - never a general bypass of the sign-in wall. It is
scoped by (page_key, item_key): page_key names which gated surface
("deep_dive", "comparison", "top100"), item_key narrows it further
when that surface has one (a ticker, a comma-joined ticker list) or is
None for a whole-page share (Top 200 has no per-tab query-param deep
link today, so its share links are page-level - see this task's own
report for that limitation).

Same storage pattern as compare_lists_store.py/email_auth.py (same
shared stocksdeepdive.db file, same RAILWAY_VOLUME_MOUNT_PATH
resolution). Tokens are generated with secrets.token_urlsafe and
stored ONLY as a sha256 hash - same convention as email_auth.py's
auth_sessions table - so the raw token is never recoverable from the
DB; it is shown to the owner exactly once, at creation time, and never
again (the admin "Share links" list shows metadata only: page, item,
created date, expiry, view count, Revoke).

Director's explicit instruction: "No visitor IP or identity stored."
resolve() below updates only view_count - no caller identity, no IP,
no timestamp-of-view column exists in this table at all.
"""

import hashlib
import os
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone

EXPIRY_CHOICES = (7, 30, 90, None)  # None = never expires
DEFAULT_EXPIRY_DAYS = 30


def _data_dir():
    return os.environ.get("RAILWAY_VOLUME_MOUNT_PATH") or os.path.dirname(__file__)


DB_PATH = os.path.join(_data_dir(), "stocksdeepdive.db")


def _conn():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS share_links (
            token_hash TEXT PRIMARY KEY,
            page_key TEXT NOT NULL,
            item_key TEXT,
            label TEXT,
            created_at TEXT NOT NULL,
            expires_at TEXT,
            revoked INTEGER NOT NULL DEFAULT 0,
            view_count INTEGER NOT NULL DEFAULT 0
        )"""
    )
    return conn


def _now():
    return datetime.now(timezone.utc)


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


def _hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def _norm_item_key(item_key):
    """None and "" are the same "no item" scope - normalized once here
    so create_link()/resolve()/list_links() never have to special-case
    which falsy value a caller happened to pass."""
    return item_key or None


def create_link(page_key, item_key=None, label=None, expiry_days=DEFAULT_EXPIRY_DAYS):
    """Mint a new share link. Returns the RAW token (show it to the
    owner now - it is never stored and can never be retrieved again).
    expiry_days: 7, 30 (default), 90, or None for "never"."""
    token = secrets.token_urlsafe(24)
    now = _now()
    expires_at = None if expiry_days is None else _iso(now + timedelta(days=expiry_days))
    with _conn() as conn:
        conn.execute(
            "INSERT INTO share_links (token_hash, page_key, item_key, label, "
            "created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?)",
            (_hash(token), page_key, _norm_item_key(item_key), label, _iso(now), expires_at),
        )
    return token


def resolve(token, page_key, item_key=None):
    """True iff `token` is a valid, non-revoked, non-expired share link
    scoped to EXACTLY this page_key/item_key - never a looser match (a
    token scoped to one ticker never resolves for another). Bumps
    view_count on a successful resolve; no identity or IP recorded."""
    if not token:
        return False
    with _conn() as conn:
        row = conn.execute(
            "SELECT page_key, item_key, expires_at, revoked FROM share_links "
            "WHERE token_hash = ?",
            (_hash(token),),
        ).fetchone()
        if not row:
            return False
        row_page_key, row_item_key, expires_at, revoked = row
        if revoked:
            return False
        if expires_at and expires_at < _iso(_now()):
            return False
        if row_page_key != page_key or row_item_key != _norm_item_key(item_key):
            return False
        conn.execute(
            "UPDATE share_links SET view_count = view_count + 1 WHERE token_hash = ?",
            (_hash(token),),
        )
    return True


def list_links(limit=500):
    """[{"token_hash":..., "page_key":..., "item_key":..., "label":...,
    "created_at":..., "expires_at":..., "revoked": bool, "view_count":...},
    ...], most-recently-created first - for the owner-only admin panel.
    token_hash (never the raw token, which isn't stored) doubles as this
    row's Revoke-button id."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT token_hash, page_key, item_key, label, created_at, "
            "expires_at, revoked, view_count FROM share_links "
            "ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [
        {
            "token_hash": r[0], "page_key": r[1], "item_key": r[2], "label": r[3],
            "created_at": r[4], "expires_at": r[5], "revoked": bool(r[6]),
            "view_count": r[7],
        }
        for r in rows
    ]


def revoke_link(token_hash):
    with _conn() as conn:
        conn.execute(
            "UPDATE share_links SET revoked = 1 WHERE token_hash = ?", (token_hash,)
        )


def cleanup():
    """Housekeeping: delete rows expired more than 30 days ago (an
    expired link is already refused by resolve(); this just keeps the
    admin list and the table itself from growing forever). Mirrors
    email_auth.cleanup()'s own "delete long-dead rows" convention."""
    cutoff = _iso(_now() - timedelta(days=30))
    with _conn() as conn:
        conn.execute(
            "DELETE FROM share_links WHERE expires_at IS NOT NULL AND expires_at < ?",
            (cutoff,),
        )
