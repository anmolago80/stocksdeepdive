"""
ai_auth_state.py

TASK 80 (Director-directed, owner-approved, 27 Nov 2026) - tracks
whether the configured ANTHROPIC_API_KEY is currently being accepted by
Anthropic, so a key rejection (401/403) is visible and alerted on
instead of silently failing every night. The incident this closes:
every nightly Top 200 AI batch from 21-26 Nov 2026 failed with "401
invalid x-api-key" (submit AND pending-batch reconcile) and nothing
told the owner for 6 nights - Selection by Value Score kept running
regardless, so companies kept entering the pool with no way to ever
get scored while the key stayed broken.

Shared by every Anthropic-call site in this codebase - top100_engine.py's
batch submit/poll/reconcile and ai_client.ask() (the one shared function
every other Ask/watchdog/Quality-on-sale feature already funnels
through) - so there's exactly ONE place that decides "is this exception
a 401/403" and ONE place that holds the resulting state, never
duplicated per caller.

State is one small JSON file, same atomic-write (.tmp + os.replace)
convention as scheduler_engine.py/db_backup_engine.py - the same
PATTERN, a different file:
  ok                    bool - whether the LAST authenticated call
                        succeeded. True until the first failure.
  first_failure_at      ISO UTC timestamp of the first call in the
                        CURRENT failure streak - None once `ok` is True
                        again (cleared by record_success(), including a
                        successful "Test AI key" button press). This is
                        what the Admin panel's red banner names.
  last_failure_at        ISO UTC timestamp of the MOST RECENT 401/403 -
                        kept even across a later success, so a still-
                        5+-nights-waiting ticker's "last run failed
                        (<date>)" caption always has a real date to
                        show (gated on `ok` being False at render time,
                        so it's never shown once the key is fixed and a
                        fresh run has tried again - see top100_render.py's
                        own use of is_failing()/last_failure_at()).
  last_status            401 | 403 | None - the HTTP status of the most
                        recent auth failure.
  last_email_sent_date   UTC date string ("YYYY-MM-DD") of the last day
                        an alert email was actually sent - at most one
                        per UTC day (item 2's own requirement), cleared
                        back to None by record_success() so a NEW
                        failure streak after a recovery emails again
                        immediately rather than waiting for the next
                        calendar day.

The key itself is NEVER logged or displayed anywhere in this module -
only the HTTP status code and the calling feature's own short label.
"""

import os
import json
from datetime import datetime, timezone


def _data_dir():
    return os.environ.get("RAILWAY_VOLUME_MOUNT_PATH") or os.path.dirname(__file__)


STATE_PATH = os.path.join(_data_dir(), "ai_auth_state.json")


def _default_state():
    return {
        "ok": True, "first_failure_at": None, "last_failure_at": None,
        "last_status": None, "last_email_sent_date": None,
    }


def _load_state():
    try:
        with open(STATE_PATH) as f:
            state = json.load(f)
    except (OSError, ValueError):
        return _default_state()
    out = _default_state()
    out.update(state)
    return out


def _save_state(state):
    try:
        tmp = STATE_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(state, f)
        os.replace(tmp, STATE_PATH)
    except OSError:
        pass


def auth_error_status(exc):
    """401 | 403 | None - the HTTP status code of `exc` if it's an
    Anthropic auth/permission error, None for anything else (a network
    error, a rate limit, a genuine 500, or any non-HTTP exception has no
    `status_code` attribute at all). `getattr(..., "status_code", None)`
    is the same safe, uniform convention this codebase already uses for
    another provider's errors (push_send.py's own `getattr(getattr(e,
    "response", None), "status_code", None)`) - anthropic.
    AuthenticationError/PermissionDeniedError both subclass APIStatusError,
    which exposes `.status_code` directly."""
    status = getattr(exc, "status_code", None)
    return status if status in (401, 403) else None


def record_failure(feature, status, log=print):
    """Call this the moment an Anthropic call fails with a 401/403 -
    logs "[ai_auth] FAILED <feature> <status>" (item 2's own required
    format), updates the persisted state (starting a new failure streak
    only if one isn't already in progress), and sends the owner's
    once-per-UTC-day alert email if one hasn't gone out today yet.
    Never raises - a failure to persist state or send the email is
    logged and swallowed, same "never let bookkeeping crash the real
    call site" discipline as every other store module in this repo."""
    log(f"[ai_auth] FAILED {feature} {status}")
    try:
        state = _load_state()
        now_iso = datetime.now(timezone.utc).isoformat()
        if state.get("ok", True) or not state.get("first_failure_at"):
            state["first_failure_at"] = now_iso
        state["ok"] = False
        state["last_failure_at"] = now_iso
        state["last_status"] = status
        _save_state(state)
    except Exception as e:
        log(f"[ai_auth] could not persist failure state (non-fatal): {e}")
    _maybe_send_alert_email(status, log=log)


def record_success(log=print):
    """Call this the moment an Anthropic call succeeds - clears the
    failure streak (first_failure_at -> None, which also clears the
    Admin panel's red banner - see banner_message()) and resets the
    once-per-day email dedup so a FUTURE failure streak emails again
    right away rather than waiting for the next calendar day. Never
    raises."""
    try:
        state = _load_state()
        state["ok"] = True
        state["first_failure_at"] = None
        state["last_email_sent_date"] = None
        _save_state(state)
    except Exception as e:
        log(f"[ai_auth] could not persist success state (non-fatal): {e}")


def is_failing():
    """True if the LAST authenticated Anthropic call failed with a
    401/403 and no call has succeeded since."""
    return not _load_state().get("ok", True)


def last_failure_at():
    """ISO UTC timestamp of the most recent 401/403, or None if there
    has never been one. Kept across a later success (unlike
    first_failure_at) so a caption naming "the last run that failed"
    still has a real date even once the key is fixed - callers that
    only want this while actively failing should check is_failing()
    first."""
    return _load_state().get("last_failure_at")


def banner_message():
    """"AI scoring failing since <UTC time>" for the Admin panel's red
    banner, or None while the key is healthy (or has never failed) -
    item 3's own exact requirement, cleared automatically the moment
    record_success() next runs (including a successful "Test AI key"
    press)."""
    state = _load_state()
    if state.get("ok", True):
        return None
    first_failure_at = state.get("first_failure_at")
    if not first_failure_at:
        return None
    try:
        dt = datetime.fromisoformat(first_failure_at)
        when = dt.strftime("%Y-%m-%d %H:%M UTC")
    except ValueError:
        when = first_failure_at
    return f"AI scoring failing since {when}"


def ai_auth_ok():
    """True/False for the nightly "[top100] coverage" log line's own
    ai_auth_ok=<True|False> field (item 4) - the CURRENT state, same
    value `is_failing()` negates."""
    return _load_state().get("ok", True)


# -----------------------------------------------------------------
# Owner alert email - same private _cfg()/_send() Mailgun convention
# every other module in this codebase keeps for itself (announce_
# engine.py/digest_engine.py/db_backup_engine.py - there is no shared
# mailgun_client.py to import).
# -----------------------------------------------------------------

def _cfg():
    domain = os.environ.get("MAILGUN_DOMAIN", "").strip()
    return {
        "api_key": os.environ.get("MAILGUN_API_KEY", "").strip(),
        "domain": domain,
        "from": os.environ.get("MAILGUN_FROM", "").strip()
                or (f"StocksDeepDive <research@{domain}>" if domain else ""),
        "base_url": (
            os.environ.get("MAILGUN_BASE_URL", "").strip()
            or os.environ.get("MAILGUN_API_BASE_URL", "").strip().removesuffix("/v3")
            or "https://api.mailgun.net"
        ).rstrip("/"),
    }


def _send_alert_email(status):
    import requests
    import ai_gate
    c = _cfg()
    if not (c["api_key"] and c["domain"]):
        return  # Mailgun not configured - nothing to send, same silent
                 # skip every other module in this codebase uses.
    resp = requests.post(
        f"{c['base_url']}/v3/{c['domain']}/messages",
        auth=("api", c["api_key"]),
        data={
            "from": c["from"], "to": [ai_gate.owner_email()],
            "subject": f"Anthropic API key rejected ({status})",
            "text": (
                f"Anthropic API key rejected ({status}) - AI scoring did not "
                "run tonight. Replace ANTHROPIC_API_KEY in Railway."
            ),
        },
        timeout=20,
    )
    resp.raise_for_status()


def _maybe_send_alert_email(status, log=print):
    """At most one alert email per UTC day (item 2's own requirement) -
    checks and updates last_email_sent_date in the SAME state file
    record_failure() just wrote, so a second 401 later the same UTC day
    (e.g. submit fails, then that night's reconcile also fails) sends
    only once. Never raises."""
    try:
        state = _load_state()
        today = datetime.now(timezone.utc).date().isoformat()
        if state.get("last_email_sent_date") == today:
            return
    except Exception as e:
        log(f"[ai_auth] could not check email dedup state (non-fatal): {e}")
        return
    try:
        _send_alert_email(status)
    except Exception as e:
        log(f"[ai_auth] alert email failed to send (non-fatal): {e}")
        return
    try:
        state = _load_state()
        state["last_email_sent_date"] = today
        _save_state(state)
    except Exception as e:
        log(f"[ai_auth] could not persist email-sent state (non-fatal): {e}")


# -----------------------------------------------------------------
# "Test AI key" - item 1's own Admin panel button.
# -----------------------------------------------------------------

def test_ai_key(log=print):
    """Makes the cheapest possible authenticated Anthropic call -
    client.models.list(limit=1), which costs nothing and needs no
    message/completion - purely to confirm the configured
    ANTHROPIC_API_KEY is accepted. Records the outcome via the SAME
    record_success()/record_failure() state machine every other
    Anthropic call site in this app uses (so a press of this button
    clears the Admin banner / starts the once-per-day email dedup
    exactly like a real nightly call would), AND separately logs
    "[ai_auth] test OK" or "[ai_auth] test FAILED <status>" (item 1's
    own exact required format, distinct from record_failure()'s own
    "[ai_auth] FAILED <feature> <status>" line). Never raises, never
    logs or displays the key itself - only its outcome. Returns
    (ok: bool, message: str) for the button's own st.success/st.error
    line."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    api_key = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
    if not api_key:
        log("[ai_auth] test FAILED no_key")
        return False, f"AI key REJECTED (no ANTHROPIC_API_KEY configured) at {now}"
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        client.models.list(limit=1)
    except Exception as e:
        status = auth_error_status(e)
        if status is not None:
            record_failure("test", status, log=log)
            log(f"[ai_auth] test FAILED {status}")
            return False, f"AI key REJECTED ({status}) at {now}"
        log(f"[ai_auth] test FAILED error: {e}")
        return False, f"AI key test could not complete (not an auth error) at {now}"
    record_success(log=log)
    log("[ai_auth] test OK")
    return True, f"AI key OK at {now}"
