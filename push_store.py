"""
push_store.py

Web Push subscription storage for installed-PWA notifications (Part 3 of
the PWA brief). Same SQLite file / volume-resolution rule as
follow_store.py and watchlist_store.py - see follow_store.py's module
docstring for why SQLite over a hand-rolled JSON file.

Identity is the signed-in email, resolved server-side from the sdd_auth
cookie (email_auth.session_email) - never anything read from the client.
A subscription is keyed by its own endpoint URL (each browser/device
install gets a distinct PushSubscription.endpoint from the browser's push
service), so one email can hold several device subscriptions (phone +
laptop, or a reinstalled PWA producing a fresh endpoint) and re-subscribing
the same device is an idempotent upsert, not a duplicate row.

Nothing here talks to a push service directly - that's server.py's
/push/subscribe /push/unsubscribe endpoints (which call these functions)
and announce_engine.py's send path (which calls pywebpush per subscription
using the rows this module returns). This module only ever does one
focused SQLite operation per call, same as follow_store.py.
"""

import json
import os
import sqlite3
from datetime import datetime, timezone


def _data_dir():
    return os.environ.get("RAILWAY_VOLUME_MOUNT_PATH") or os.path.dirname(__file__)


DB_PATH = os.path.join(_data_dir(), "stocksdeepdive.db")


def _conn():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS push_subscriptions (
            endpoint TEXT PRIMARY KEY,
            email TEXT NOT NULL,
            subscription_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        )"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_push_email ON push_subscriptions(email)"
    )
    return conn


def subscribe(email, subscription):
    """Store (or refresh) one device's PushSubscription for this email.
    `subscription` is the raw dict the browser's pushManager.subscribe()
    returned (endpoint + keys.p256dh + keys.auth) - stored verbatim as
    JSON since pywebpush wants that exact shape back at send time."""
    if not email or not isinstance(subscription, dict):
        return False
    endpoint = (subscription.get("endpoint") or "").strip()
    if not endpoint or len(endpoint) > 2048:
        return False
    with _conn() as conn:
        conn.execute(
            "INSERT INTO push_subscriptions (endpoint, email, subscription_json, created_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(endpoint) DO UPDATE SET email=excluded.email, "
            "subscription_json=excluded.subscription_json",
            (endpoint, email.strip().lower(), json.dumps(subscription),
             datetime.now(timezone.utc).isoformat()),
        )
    return True


def unsubscribe(endpoint):
    if not endpoint:
        return
    with _conn() as conn:
        conn.execute(
            "DELETE FROM push_subscriptions WHERE endpoint = ?", (endpoint,)
        )


def endpoint_owner(endpoint):
    """The email a stored endpoint belongs to, or None - used by
    server.py's /push/unsubscribe to make sure a signed-in visitor can
    only ever unsubscribe their OWN device, never one they merely guessed
    the endpoint URL for."""
    if not endpoint:
        return None
    with _conn() as conn:
        row = conn.execute(
            "SELECT email FROM push_subscriptions WHERE endpoint = ?",
            (endpoint,),
        ).fetchone()
    return row[0] if row else None


def prune(endpoint):
    """Same as unsubscribe - separate name for the call site that matters:
    a push send got a 404/410 back from the push service, meaning the
    browser itself has invalidated this endpoint. Kept as its own function
    so that call site reads as "this subscription is dead", not "the user
    asked to stop"."""
    unsubscribe(endpoint)


def subscriptions_for(email):
    """This email's stored subscriptions, each as the dict pywebpush needs
    (i.e. exactly what pushManager.subscribe() produced)."""
    if not email:
        return []
    with _conn() as conn:
        rows = conn.execute(
            "SELECT subscription_json FROM push_subscriptions WHERE email = ?",
            (email.strip().lower(),),
        ).fetchall()
    out = []
    for (raw,) in rows:
        try:
            out.append(json.loads(raw))
        except (TypeError, ValueError):
            pass
    return out


def has_subscription(email):
    if not email:
        return False
    with _conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM push_subscriptions WHERE email = ? LIMIT 1",
            (email.strip().lower(),),
        ).fetchone()
    return row is not None


def subscription_count():
    """Aggregate stats only, same shape as follow_store.follow_count()."""
    with _conn() as conn:
        emails = conn.execute(
            "SELECT COUNT(DISTINCT email) FROM push_subscriptions"
        ).fetchone()[0]
        rows = conn.execute("SELECT COUNT(*) FROM push_subscriptions").fetchone()[0]
    return {"emails": emails, "devices": rows}
