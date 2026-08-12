"""
feedback_engine.py

Optional "Tell us what you think" button shown top-right on the three main
service pages (Deep Dive, Comparison, Rational Compounder Analysis).
Visitors type a short message - plus, if they aren't signed in, an optional
email for a reply - and it's emailed straight to the site's own inbox via
SMTP. Completely separate from paywall_engine.py: this works regardless of
whether Google sign-in or the subscription paywall are configured at all.

DORMANT BY DEFAULT, same rule as paywall_engine.py: until the Railway
variables below are set, render_feedback_button() renders nothing at all -
no half-built button, no broken form for visitors to hit.

REQUIRED RAILWAY VARIABLES (all optional until Andrew is ready):
  FEEDBACK_SMTP_EMAIL          the mailbox's own address, e.g.
                                rationalcompounder@stocksdeepdive.com - used
                                both to log in to send mail AND, unless
                                FEEDBACK_TO_EMAIL is set separately, as
                                where feedback lands. This has to be a real
                                hosted mailbox (Google Workspace, Zoho Mail,
                                Microsoft 365, etc.) with SMTP access, not
                                just free forwarding - forwarding-only
                                addresses can receive mail but can't send.
  FEEDBACK_SMTP_APP_PASSWORD   an app password for that mailbox (not the
                                normal login password - Google Workspace,
                                Zoho, and Microsoft 365 all have an
                                equivalent "app password" setting once
                                2FA/MFA is turned on for the mailbox).
  FEEDBACK_SMTP_HOST            defaults to smtp.gmail.com (works for both
                                personal Gmail and Google Workspace mail).
                                Override if the mailbox is hosted elsewhere
                                (e.g. smtp.zoho.com, smtp.office365.com).
  FEEDBACK_SMTP_PORT            defaults to 465 (SSL).
  FEEDBACK_TO_EMAIL             optional - only needed if feedback should
                                land somewhere OTHER than
                                FEEDBACK_SMTP_EMAIL itself.

FAILS SAFE: if sending fails for any reason (bad credentials, SMTP outage,
not configured yet), the visitor sees a plain "couldn't send, try again"
message - never a stack trace - and their typed message stays in the box so
nothing is lost.
"""

import os
import smtplib
from datetime import datetime, timezone
from email.mime.text import MIMEText

import streamlit as st


def _smtp_env():
    return {
        "email": os.environ.get("FEEDBACK_SMTP_EMAIL", "").strip(),
        "app_password": os.environ.get("FEEDBACK_SMTP_APP_PASSWORD", "").strip(),
        "host": os.environ.get("FEEDBACK_SMTP_HOST", "").strip() or "smtp.gmail.com",
        "port": int((os.environ.get("FEEDBACK_SMTP_PORT", "").strip() or "465")),
        "to_email": os.environ.get("FEEDBACK_TO_EMAIL", "").strip(),
    }


def _smtp_configured():
    env = _smtp_env()
    return bool(env["email"] and env["app_password"])


def send_feedback(page_label, message, reply_to=None):
    """
    Emails one feedback message via SMTP. Returns True on success, False on
    any failure - never raises, so callers show a generic retry message
    rather than a technical error.
    """
    if not message or not message.strip() or not _smtp_configured():
        return False
    env = _smtp_env()
    to_email = env["to_email"] or env["email"]
    try:
        body_lines = [
            f"Page: {page_label}",
            f"When: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
            f"From: {reply_to or '(not signed in / no email given)'}",
            "",
            message.strip(),
        ]
        msg = MIMEText("\n".join(body_lines))
        msg["Subject"] = f"StocksDeepDive feedback - {page_label}"
        msg["From"] = env["email"]
        msg["To"] = to_email
        if reply_to:
            msg["Reply-To"] = reply_to

        with smtplib.SMTP_SSL(env["host"], env["port"], timeout=10) as server:
            server.login(env["email"], env["app_password"])
            server.sendmail(env["email"], [to_email], msg.as_string())
        return True
    except Exception:
        return False


def render_feedback_button(page_label, key_prefix, user_email=None):
    """
    Renders a right-aligned "Tell us what you think" button that expands
    into a small feedback form. Renders nothing at all if SMTP isn't
    configured yet (see module docstring) - stays fully invisible until
    Andrew adds the mailbox credentials to Railway.

    page_label: shown in the email subject/body so it's clear which service
        the feedback is about (e.g. "Deep Dive").
    key_prefix: unique-ish prefix for this page's widget keys.
    user_email: the visitor's signed-in email if known (pass
        paywall_engine.current_user_email()) - auto-attached so a reply
        doesn't require asking who sent it. Works independently of
        whether the subscription paywall itself is turned on.
    """
    if not _smtp_configured():
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
