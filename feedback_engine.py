"""
feedback_engine.py

Optional "Tell us what you think" button shown top-right on the three main
service pages (Deep Dive, Comparison, Rational Compounder Analysis).
Visitors type a short message - plus, if they aren't signed in, an optional
email for a reply - and it's emailed via Resend (a transactional email API,
https://resend.com). Completely separate from paywall_engine.py: this works
regardless of whether Google sign-in or the subscription paywall are
configured at all.

WHY RESEND INSTEAD OF A REAL MAILBOX: Resend only needs domain ownership
proved once via a couple of DNS records (SPF/DKIM) - it doesn't need an
actual inbox to send FROM. The intended pairing is Cloudflare Email Routing
(free) handling the RECEIVING side - forwarding anything sent to
rationalcompounder@stocksdeepdive.com to an inbox Andrew already checks -
while Resend handles the SENDING side from the app. Together that gives a
fully working rationalcompounder@stocksdeepdive.com address without paying
for or managing a full hosted mailbox.

DORMANT BY DEFAULT, same rule as paywall_engine.py: until the Railway
variables below are set, render_feedback_button() renders nothing at all -
no half-built button, no broken form for visitors to hit.

REQUIRED RAILWAY VARIABLES (all optional until Andrew is ready):
  RESEND_API_KEY        API key from the Resend dashboard (Resend ->
                         API Keys -> Create API Key).
  FEEDBACK_FROM_EMAIL    the "From" address Resend sends as, e.g.
                         "StocksDeepDive <rationalcompounder@stocksdeepdive.com>"
                         or a plain address. Must be on a domain that's
                         been verified in Resend (Resend -> Domains ->
                         add stocksdeepdive.com -> add the SPF/DKIM
                         records it shows you to your DNS).
  FEEDBACK_TO_EMAIL      where feedback should land, e.g.
                         rationalcompounder@stocksdeepdive.com (if Cloudflare
                         Email Routing is forwarding that to a real inbox)
                         or any inbox directly.

FAILS SAFE: if sending fails for any reason (bad API key, domain not
verified yet, Resend outage, not configured yet), the visitor sees a plain
"couldn't send, try again" message - never a stack trace - and their typed
message stays in the box so nothing is lost.
"""

import os
from datetime import datetime, timezone

import requests
import streamlit as st

_RESEND_API_URL = "https://api.resend.com/emails"


def _resend_env():
    return {
        "api_key": os.environ.get("RESEND_API_KEY", "").strip(),
        "from_email": os.environ.get("FEEDBACK_FROM_EMAIL", "").strip(),
        "to_email": os.environ.get("FEEDBACK_TO_EMAIL", "").strip(),
    }


def _resend_configured():
    env = _resend_env()
    return bool(env["api_key"] and env["from_email"] and env["to_email"])


def send_feedback(page_label, message, reply_to=None):
    """
    Emails one feedback message via the Resend API. Returns True on
    success, False on any failure - never raises, so callers show a
    generic retry message rather than a technical error.
    """
    if not message or not message.strip() or not _resend_configured():
        return False
    env = _resend_env()
    try:
        body_lines = [
            f"Page: {page_label}",
            f"When: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
            f"From: {reply_to or '(not signed in / no email given)'}",
            "",
            message.strip(),
        ]
        payload = {
            "from": env["from_email"],
            "to": [env["to_email"]],
            "subject": f"StocksDeepDive feedback - {page_label}",
            "text": "\n".join(body_lines),
        }
        if reply_to:
            payload["reply_to"] = reply_to

        resp = requests.post(
            _RESEND_API_URL,
            headers={
                "Authorization": f"Bearer {env['api_key']}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=10,
        )
        return resp.status_code in (200, 201)
    except Exception:
        return False


def render_feedback_button(page_label, key_prefix, user_email=None):
    """
    Renders a right-aligned "Tell us what you think" button that expands
    into a small feedback form. Renders nothing at all if Resend isn't
    configured yet (see module docstring) - stays fully invisible until
    Andrew adds the credentials to Railway.

    page_label: shown in the email subject/body so it's clear which service
        the feedback is about (e.g. "Deep Dive").
    key_prefix: unique-ish prefix for this page's widget keys.
    user_email: the visitor's signed-in email if known (pass
        paywall_engine.current_user_email()) - auto-attached so a reply
        doesn't require asking who sent it. Works independently of
        whether the subscription paywall itself is turned on.
    """
    if not _resend_configured():
        return

    st.markdown(
        """
        <style>
        [class*="st-key-fb_popover_"] button {
            background-color: #ffffff !important;
            color: #0d9488 !important;
            border: 1.5px solid #0d9488 !important;
            border-radius: 999px !important;
            padding: 4px 16px !important;
            font-weight: 600 !important;
            font-size: 13px !important;
            box-shadow: none !important;
        }
        [class*="st-key-fb_popover_"] button:hover {
            background-color: #0d9488 !important;
            color: #ffffff !important;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )

    _sp, _c1 = st.columns([9.5, 2.5], gap="small")
    with _c1:
        with st.popover(
            "\U0001F4AC Tell us what you think",
            key=f"fb_popover_{key_prefix}",
            use_container_width=True,
        ):
            st.caption(f"Feedback on {page_label} - goes straight to our inbox.")
            msg_key = f"fb_msg_{key_prefix}"
            email_key = f"fb_email_{key_prefix}"
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
                    st.session_state[msg_key] = ""
                    st.toast("Thanks - feedback sent!", icon="✅")
                    st.rerun()
                else:
                    st.error("Couldn't send just now - please try again in a moment.")
