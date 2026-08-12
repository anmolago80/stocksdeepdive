"""
feedback_engine.py

Optional "Tell us what you think" button shown top-right on the three main
service pages (Deep Dive, Comparison, Rational Compounder Analysis).
Visitors type a short message - plus, if they aren't signed in, an optional
email for a reply - and it's emailed via Mailgun's HTTP API. Completely
separate from paywall_engine.py: this works regardless of whether Google
sign-in or the subscription paywall are configured at all.

WHY MAILGUN (HTTP API) INSTEAD OF SMTP: direct SMTP (smtp.gmail.com etc.)
does not work here - Railway blocks outbound SMTP traffic (ports 25/465/587)
on the Free, Trial, and Hobby plans to prevent spam abuse, which is why the
original Gmail SMTP version failed in production with
"OSError: [Errno 101] Network is unreachable". Mailgun's HTTPS API sends
mail over normal port 443, which Railway does not block, and its free tier
(100 emails/day, no credit card) does not run into the "one verified
sending domain" cap that ruled out Resend (already used for another
project/domain).

The mail lands wherever FEEDBACK_TO_EMAIL points - e.g.
rationalcompounder@stocksdeepdive.com, forwarded via Cloudflare Email
Routing to a real inbox - or directly at a personal inbox if not.

DORMANT BY DEFAULT, same rule as paywall_engine.py: until the Railway
variables below are set, render_feedback_button() renders nothing at all -
no half-built button, no broken form for visitors to hit.

REQUIRED RAILWAY VARIABLES (all optional until Andrew is ready):
  MAILGUN_API_KEY        the private API key from the Mailgun dashboard
                          (Settings -> API Keys).
  MAILGUN_DOMAIN          the sending domain verified in Mailgun, e.g.
                          mg.stocksdeepdive.com (a subdomain is the usual
                          setup so it doesn't touch the main domain's
                          existing DNS records).
  FEEDBACK_TO_EMAIL       where feedback should land, e.g.
                          rationalcompounder@stocksdeepdive.com (forwarded
                          via Cloudflare Email Routing) or any inbox
                          directly.
  MAILGUN_FROM_EMAIL      optional, defaults to
                          "StocksDeepDive Feedback <feedback@{MAILGUN_DOMAIN}>".
  MAILGUN_API_BASE_URL    optional, defaults to https://api.mailgun.net/v3
                          (use https://api.eu.mailgun.net/v3 for an
                          EU-region Mailgun domain).

FAILS SAFE: if sending fails for any reason (bad API key, domain not yet
verified, Mailgun outage, not configured yet), the visitor sees a plain
"couldn't send, try again" message - never a stack trace - and their typed
message stays in the box so nothing is lost. The real reason is printed to
Railway's logs (search for "[feedback_engine]") so a failure can actually
be diagnosed.
"""

import os
from datetime import datetime, timezone

import requests
import streamlit as st


def _mailgun_env():
    domain = os.environ.get("MAILGUN_DOMAIN", "").strip()
    return {
        "api_key": os.environ.get("MAILGUN_API_KEY", "").strip(),
        "domain": domain,
        "base_url": os.environ.get("MAILGUN_API_BASE_URL", "").strip()
        or "https://api.mailgun.net/v3",
        "from_email": os.environ.get("MAILGUN_FROM_EMAIL", "").strip()
        or (f"StocksDeepDive Feedback <feedback@{domain}>" if domain else ""),
        "to_email": os.environ.get("FEEDBACK_TO_EMAIL", "").strip(),
    }


def _mailgun_configured():
    env = _mailgun_env()
    return bool(env["api_key"] and env["domain"] and env["to_email"])


def send_feedback(page_label, message, reply_to=None):
    """
    Emails one feedback message via Mailgun's HTTP API. Returns True on
    success, False on any failure - never raises, so callers show a
    generic retry message rather than a technical error.
    """
    if not message or not message.strip() or not _mailgun_configured():
        return False
    env = _mailgun_env()
    try:
        body_lines = [
            f"Page: {page_label}",
            f"When: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
            f"From: {reply_to or '(not signed in / no email given)'}",
            "",
            message.strip(),
        ]
        data = {
            "from": env["from_email"],
            "to": env["to_email"],
            "subject": f"StocksDeepDive feedback - {page_label}",
            "text": "\n".join(body_lines),
        }
        if reply_to:
            data["h:Reply-To"] = reply_to

        resp = requests.post(
            f"{env['base_url']}/{env['domain']}/messages",
            auth=("api", env["api_key"]),
            data=data,
            timeout=10,
        )
        resp.raise_for_status()
        return True
    except Exception as e:
        # The visitor only ever sees a generic "couldn't send" message (see
        # module docstring) - this print is the only place the real reason
        # is visible, in Railway's logs, so a failure can actually be
        # diagnosed instead of just silently swallowed.
        print(f"[feedback_engine] send_feedback failed: {type(e).__name__}: {e}")
        return False


def is_configured():
    """Public check so callers (e.g. app.py deciding whether to reserve a
    spot for this widget in another row) can tell whether the button will
    render anything at all, without reaching into a private function."""
    return _mailgun_configured()


_FEEDBACK_BUTTON_CSS = """
<style>
/* Streamlit renders a st.popover's trigger ~1rem lower inside its column
   than a plain st.button (an internal spacing quirk of the popover
   widget) - when this sits beside "Sign out" (a plain button) in the
   account bar row, that extra 1rem makes the two visibly misaligned.
   Pull the whole popover wrapper back up so both buttons share the same
   baseline. */
[class*="st-key-fb_popover_"] {
    margin-top: -1rem !important;
}
[class*="st-key-fb_popover_"] button {
    background-color: #ffffff !important;
    color: #0d9488 !important;
    border: 1.5px solid #0d9488 !important;
    border-radius: 999px !important;
    padding: 4px 16px !important;
    font-weight: 600 !important;
    font-size: 13px !important;
    box-shadow: none !important;
    width: fit-content !important;
    min-width: 0 !important;
    white-space: nowrap !important;
}
[class*="st-key-fb_popover_"] button p {
    white-space: nowrap !important;
}
[class*="st-key-fb_popover_"] button:hover {
    background-color: #0d9488 !important;
    color: #ffffff !important;
}
</style>
"""


def render_feedback_widget(page_label, key_prefix, user_email=None):
    """
    Renders just the "Tell us what you think" popover button + form -
    no row/columns of its own, so a caller (e.g. paywall_engine's account
    bar) can place it inside an existing row next to other controls.
    Renders nothing at all if Mailgun isn't configured yet (see module
    docstring) - stays fully invisible until Andrew adds the Mailgun
    variables to Railway.

    page_label: shown in the email subject/body so it's clear which service
        the feedback is about (e.g. "Deep Dive").
    key_prefix: unique-ish prefix for this page's widget keys.
    user_email: the visitor's signed-in email if known (pass
        paywall_engine.current_user_email()) - auto-attached so a reply
        doesn't require asking who sent it. Works independently of
        whether the subscription paywall itself is turned on.
    """
    if not _mailgun_configured():
        return

    st.markdown(_FEEDBACK_BUTTON_CSS, unsafe_allow_html=True)

    msg_key = f"fb_msg_{key_prefix}"
    email_key = f"fb_email_{key_prefix}"
    sent_flag_key = f"fb_sent_{key_prefix}"

    # Streamlit forbids writing to a widget's session_state key after that
    # widget has already been instantiated in the current run - so clearing
    # the text box after a successful send has to happen here, BEFORE
    # st.text_area(key=msg_key, ...) below runs, not inside the button's
    # on-click handling (which fires after the text_area already exists for
    # this run). The button handler just sets sent_flag_key and reruns;
    # this block does the actual clearing + success toast on the next run.
    if st.session_state.pop(sent_flag_key, False):
        st.session_state[msg_key] = ""
        st.toast("Thanks - feedback sent!", icon="✅")

    with st.popover(
        "\U0001F4AC Tell us what you think",
        key=f"fb_popover_{key_prefix}",
    ):
        st.caption(f"Feedback on {page_label} - goes straight to our inbox.")
        st.text_area(
            "Your feedback",
            key=msg_key,
            label_visibility="collapsed",
            placeholder="What's working, what's confusing, what would you change?",
            height=100,
        )
        if not user_email:
            st.text_input(
                "Your email (optional, if you'd like a reply)",
                key=email_key,
                placeholder="you@example.com",
            )
        if st.button("Send", key=f"fb_send_{key_prefix}", type="primary"):
            _text = st.session_state.get(msg_key, "")
            _reply_to = user_email or (st.session_state.get(email_key, "") or "").strip() or None
            if not _text.strip():
                st.warning("Type a message first.")
            elif send_feedback(page_label, _text, reply_to=_reply_to):
                st.session_state[sent_flag_key] = True
                st.rerun()
            else:
                st.error("Couldn't send just now - please try again in a moment.")


def render_feedback_button(page_label, key_prefix, user_email=None):
    """
    Standalone version of render_feedback_widget() that creates its own
    right-aligned row - kept for any caller that isn't embedding this
    inside another row (the account bar in paywall_engine.py embeds
    render_feedback_widget() directly instead, via render_account_bar's
    extra_widget param).
    """
    if not _mailgun_configured():
        return
    _sp, _c1 = st.columns([10.3, 1.7], gap="small")
    with _c1:
        render_feedback_widget(page_label, key_prefix, user_email=user_email)
