"""
ai_gate.py

THE gate every AI feature in this codebase must call before making an
Anthropic API call - the AI-readiness roadmap's (AI_ROADMAP_
stocksdeepdive.md) own non-negotiable ground rule is "never let an AI
call run without the gate." Plays the same role for AI features that
paywall_engine.render_gate() plays for subscription content: one
function, called first, that decides yes/no and explains why not -
except this module is plain Python with no Streamlit import, so it also
works from a background job (Phase 5's nightly portfolio watchdog runs
from scheduler_engine.py, not a live browser session) and not only from
app.py's interactive Ask boxes.

TIERS (owner-set defaults below, all editable at runtime via the admin
AI settings panel -> ai_settings_store):
  owner  - AI_OWNER_EMAIL (defaults to anmolago@hotmail.com, the site
           owner's own account) - unlimited, and exempt even from the
           site-wide spend cap below.
  plus   - an active Stripe subscription, via paywall_engine's EXISTING
           gate hooks (paywall_engine.is_subscribed()) - the roadmap
           calls for reusing that subscription rather than standing up
           a second Stripe price just for AI quota - plus_daily_limit /
           plus_monthly_limit questions.
  free   - signed in, not subscribed - free_daily_limit questions/day.
  None   - not signed in at all. Always denied: a daily/monthly quota
           needs a stable identity to mean anything, and this app
           already has a working sign-in flow (paywall_engine +
           email_auth) every other per-user feature (My Portfolio) also
           requires.

SITE-WIDE SPEND CAP: checked before any per-user quota, and blocks
EVERYONE except the owner once this UTC month's recorded spend
(ai_usage_store.spend_this_month()) reaches monthly_spend_cap_usd - a
circuit breaker so a quota misconfiguration or a burst of Plus
subscribers can never run up an unbounded Anthropic bill unattended.
The admin panel shows this same number as a live meter with 80%/100%
visual thresholds (green/amber/red) - see app.py's
_render_ai_admin_panel.

FAILS CLOSED, same deliberate exception paywall_engine.is_subscribed()
documents for itself: if settings or usage can't be read for any
reason, check() denies rather than allows. A DB hiccup should mean "AI
features unavailable for a moment," not "give away free API calls."
"""

import os

import ai_settings_store
import ai_usage_store
import paywall_engine


def _owner_email():
    return (os.environ.get("AI_OWNER_EMAIL", "").strip()
            or "anmolago@hotmail.com").strip().lower()


def is_owner(email):
    return bool(email) and email.strip().lower() == _owner_email()


def tier_for(email):
    """'owner' | 'plus' | 'free' | None (not signed in). Never raises -
    a Stripe lookup failure (paywall_engine.is_subscribed already fails
    closed itself) just falls back to 'free' rather than blocking tier
    resolution entirely."""
    email = (email or "").strip()
    if not email:
        return None
    if is_owner(email):
        return "owner"
    try:
        if paywall_engine.is_subscribed(email):
            return "plus"
    except Exception:
        pass
    return "free"


def check(email, feature):
    """(allowed: bool, message: str, tier: str|None).

    `message` is always a short, user-facing sentence - safe to show
    directly under the Ask box whether allowed or not (empty string on
    success, since callers have nothing to tell the visitor then).
    `feature` is a short slug (e.g. "deep_dive_ask", "portfolio_ask")
    logged for the admin panel's per-feature breakdown - it does not
    create a separate quota pool; every feature shares one pool per the
    roadmap's plain "N questions/day" wording."""
    tier = tier_for(email)
    if tier is None:
        return False, "Sign in to ask a question.", None
    if tier == "owner":
        return True, "", tier

    try:
        settings = ai_settings_store.get_settings()
    except Exception:
        return False, "AI features are temporarily unavailable - please try again shortly.", tier

    try:
        spend = ai_usage_store.spend_this_month()
    except Exception:
        return False, "AI features are temporarily unavailable - please try again shortly.", tier
    if spend >= settings["monthly_spend_cap_usd"]:
        return False, "AI features have reached this month's usage cap - back next month.", tier

    try:
        today = ai_usage_store.questions_today(email)
    except Exception:
        return False, "AI features are temporarily unavailable - please try again shortly.", tier

    if tier == "plus":
        try:
            this_month = ai_usage_store.questions_this_month(email)
        except Exception:
            return False, "AI features are temporarily unavailable - please try again shortly.", tier
        if this_month >= settings["plus_monthly_limit"]:
            return False, (
                f"You've used all {settings['plus_monthly_limit']} questions "
                "included this month - resets on the 1st."
            ), tier
        if today >= settings["plus_daily_limit"]:
            return False, (
                f"You've reached today's limit of {settings['plus_daily_limit']} "
                "questions - back tomorrow."
            ), tier
        return True, "", tier

    # free
    if today >= settings["free_daily_limit"]:
        return False, (
            f"You've used your {settings['free_daily_limit']} free questions "
            "today - come back tomorrow, or subscribe for 300/month."
        ), tier
    return True, "", tier


def remaining(email):
    """{'tier', 'today_used', 'today_limit', 'month_used', 'month_limit'}
    for the small "N questions left today" caption under an Ask box -
    UI-only convenience, never used for the allow/deny decision itself
    (check() above is the only function that decides that). *_limit is
    None for the owner tier (unlimited). Never raises - returns tier
    'free' with zero-filled counts on any read error, since this is
    purely informational and must never crash the box it's shown under."""
    tier = tier_for(email)
    if tier is None:
        return {"tier": None, "today_used": 0, "today_limit": 0,
                "month_used": 0, "month_limit": 0}
    if tier == "owner":
        return {"tier": "owner", "today_used": 0, "today_limit": None,
                "month_used": 0, "month_limit": None}
    try:
        settings = ai_settings_store.get_settings()
        today_used = ai_usage_store.questions_today(email)
    except Exception:
        return {"tier": tier, "today_used": 0, "today_limit": 0,
                "month_used": 0, "month_limit": 0}
    if tier == "plus":
        try:
            month_used = ai_usage_store.questions_this_month(email)
        except Exception:
            month_used = 0
        return {"tier": "plus", "today_used": today_used,
                "today_limit": settings["plus_daily_limit"],
                "month_used": month_used,
                "month_limit": settings["plus_monthly_limit"]}
    return {"tier": "free", "today_used": today_used,
            "today_limit": settings["free_daily_limit"],
            "month_used": 0, "month_limit": 0}


def record(email, feature, model, input_tokens, output_tokens, cost_usd):
    """Call after ai_client.ask() whenever real tokens were spent - i.e.
    whenever input_tokens or output_tokens is nonzero, EVEN IF the call's
    own ok flag was False (an empty-text response still cost real
    Anthropic tokens and must still count toward the spend cap; it just
    didn't produce an answer worth showing). Never call this for a
    request check() already blocked, or one ai_client.ask() rejected
    before ever reaching Anthropic (no key configured, package missing)
    - nothing was spent in either case.

    Wrapped in try/except by the caller, same convention as every other
    store write in this app - a logging failure must never surface as a
    visible error over an answer the visitor already received."""
    ai_usage_store.record_usage(email, feature, model, input_tokens,
                                output_tokens, cost_usd)
