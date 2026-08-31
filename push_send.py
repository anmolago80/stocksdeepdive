"""
push_send.py

The one place that actually calls pywebpush.webpush() - shared by
server.py's "send myself a test notification" endpoint and
announce_engine.py's research-update push (run alongside its existing
Mailgun email send, not instead of it). Both need identical logic: send
one payload to every stored subscription for an email, prune any endpoint
the push service reports as permanently gone (404/410 - the browser
itself invalidated it, not a transient failure), and never let one bad
subscription stop the rest - same per-recipient try/except failure model
as announce_engine.py's email send.

CONFIG (Railway environment variables):
  VAPID_PUBLIC_KEY    required. Also handed to the browser at
                       GET /push/vapid-public-key (server.py) as the
                       applicationServerKey for pushManager.subscribe() -
                       the browser and this module must be using the SAME
                       keypair or every subscribe/send silently fails.
  VAPID_PRIVATE_KEY   required. PEM-encoded EC private key. Generate once
                       locally (see the instructions server.py's boot log
                       prints when these are unset) - never committed,
                       set directly on Railway.
  VAPID_CLAIMS_EMAIL  required. A contact address push services may use
                       to reach the site owner about this key - becomes
                       the VAPID JWT's "sub" claim (mailto:...), never
                       shown to end users.

If pywebpush itself (or one of its native deps) fails to import, or the
three env vars above aren't all set, push becomes a clean no-op
everywhere it's called from - never a boot failure, never a 500 for the
visitor who happened to trigger a send.
"""

import json
import logging
import os

import push_store

logger = logging.getLogger("sdd.push")

try:
    from pywebpush import WebPushException, webpush
except Exception:  # pragma: no cover - sandbox/deploy dependency edge
    webpush = None
    WebPushException = Exception


def _vapid_cfg():
    return {
        "public_key": os.environ.get("VAPID_PUBLIC_KEY", "").strip(),
        "private_key": os.environ.get("VAPID_PRIVATE_KEY", "").strip(),
        "claims_email": os.environ.get("VAPID_CLAIMS_EMAIL", "").strip(),
    }


def is_configured():
    c = _vapid_cfg()
    return bool(webpush and c["public_key"] and c["private_key"] and c["claims_email"])


def send_to_email(email, title, body, url="/"):
    """Push `title`/`body` (+ a `url` the notificationclick handler opens)
    to every device subscription stored for this email. Best-effort per
    subscription. Returns {'sent': n, 'pruned': n, 'errors': n}."""
    summary = {"sent": 0, "pruned": 0, "errors": 0}
    if not is_configured():
        logger.info("[push] VAPID not configured - skipping push to %s", email)
        return summary

    c = _vapid_cfg()
    payload = json.dumps({"title": title, "body": body, "url": url})
    for sub in push_store.subscriptions_for(email):
        try:
            webpush(
                subscription_info=sub,
                data=payload,
                vapid_private_key=c["private_key"],
                vapid_claims={"sub": f"mailto:{c['claims_email']}"},
            )
            summary["sent"] += 1
        except WebPushException as e:
            status = getattr(getattr(e, "response", None), "status_code", None)
            if status in (404, 410):
                # The push service itself says this endpoint is gone for
                # good (uninstalled PWA, revoked permission, ...) - prune
                # it so future sends don't keep paying for the failure.
                push_store.prune(sub.get("endpoint", ""))
                summary["pruned"] += 1
            else:
                summary["errors"] += 1
                logger.warning("[push] send to %s failed: %s", email, e)
        except Exception as e:
            summary["errors"] += 1
            logger.warning("[push] send to %s failed: %s", email, e)
    return summary
