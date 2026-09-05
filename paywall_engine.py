"""
paywall_engine.py

Optional subscription gate for the three "premium" surfaces of this app:
Deep Dive's detailed breakdown, Comparison's full column set, and the
Research (Rational Compounder Analysis) page's Company Potential / Fair
Value sections. Everything else (Home, Stock Scanner, Deep Dive's preview,
Comparison's identity+signal columns, and Research's other sections) stays
free regardless of this module.

MASTER SWITCH: PAYWALL_ENABLED (Railway environment variable). Unset or
anything other than "true"/"1"/"yes"/"on" (case-insensitive) means
render_gate() always returns True (nothing ever locks) and the account bar
never shows a Subscribe button or calls Stripe - PAYWALL_ENABLED is
specifically the SUBSCRIPTION switch, not a switch for the whole module.
This is deliberate: the feature ships live in the code now, but stays
completely dormant until Andrew is ready to charge and flips this one
variable himself. No redeploy needed to activate it - Railway restarts the
service when env vars change, which re-reads this at next startup.

SIGN-IN IS SEPARATE FROM PAYWALL_ENABLED: render_account_bar() shows a
"Sign In" button (or the signed-in name + "Sign out") as soon as Google
sign-in is configured (see _auth_configured() below), regardless of
whether PAYWALL_ENABLED is set - so Andrew can start building a signed-in
audience, and get feedback attributed to a real email via
feedback_engine.py, well before subscriptions themselves ever turn on.

HOW IT WORKS ONCE ENABLED:
  1. Google sign-in via Streamlit's own built-in st.login()/st.user (OIDC),
     not a separate auth vendor. Needs a Google Cloud OAuth Client ID -
     see the setup checklist delivered alongside this file.
  2. Subscription status is checked directly against the Stripe API by
     email (Customer + active Subscription lookup), cached for an hour -
     not a webhook. Streamlit doesn't have a natural place to host a
     webhook receiver, and a subscription status doesn't need to update
     instantly, so polling Stripe on login/periodically is simpler
     infrastructure for the same result.
  3. render_gate() is the one function every gated section calls. It
     returns True (render the real content) or False (it already rendered
     a login/subscribe prompt in place of the content, so the caller
     should skip rendering and return/continue).

FAIL-CLOSED, not fail-open: every other optional integration in this app
(NewsAPI, Google Trends, StockTwits, yfinance extras) fails SAFE to a
default/zero on any error, because giving up on a "nice to have" signal is
harmless. The subscription check below is the one deliberate exception -
if the Stripe API call itself fails (network issue, bad key, Stripe
outage), is_subscribed() returns False, i.e. treats the visitor as NOT
subscribed. Failing open here would mean a Stripe hiccup gives away paid
content for free; failing closed means a Stripe hiccup shows a real
subscriber a "please try again" state instead - the safer direction for a
paywall specifically.
"""

import html
import json
import os

import streamlit as st

import email_auth


# -----------------------------------
# MASTER SWITCH
# -----------------------------------

def _truthy(v):
    return (v or "").strip().lower() in ("1", "true", "yes", "on")


PAYWALL_ENABLED = _truthy(os.environ.get("PAYWALL_ENABLED"))

_DEFAULT_REDIRECT_URI = "https://stocksdeepdive.com/oauth2callback"


# -----------------------------------
# CONFIG CHECKS - so a half-configured PAYWALL_ENABLED=true (e.g. mid-setup)
# degrades to an informative message instead of crashing the page.
# -----------------------------------

def _auth_env():
    return {
        "client_id": os.environ.get("GOOGLE_OAUTH_CLIENT_ID", "").strip(),
        "client_secret": os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET", "").strip(),
        "cookie_secret": os.environ.get("AUTH_COOKIE_SECRET", "").strip(),
        "redirect_uri": os.environ.get("AUTH_REDIRECT_URI", "").strip() or _DEFAULT_REDIRECT_URI,
    }


def _auth_configured():
    env = _auth_env()
    return bool(env["client_id"] and env["client_secret"] and env["cookie_secret"])


def _stripe_configured():
    return bool(
        os.environ.get("STRIPE_SECRET_KEY", "").strip()
        and os.environ.get("STRIPE_PRICE_ID", "").strip()
    )


@st.cache_resource(show_spinner=False)
def _ensure_auth_secrets_written():
    """
    st.login()/st.user need Streamlit's own secrets.toml [auth] block -
    there's no built-in env-var fallback for that section the way this
    app's own code does for e.g. ANTHROPIC_API_KEY. Rather than committing
    real OAuth credentials into the git repo (never do that - same reason
    every other secret in this app lives in Railway's Variables instead),
    this writes a local .streamlit/secrets.toml from the Railway
    environment variables once per process, before anything tries to call
    st.login(). @st.cache_resource makes this run exactly once per server
    process regardless of how many times/sessions call it.

    No-op (returns False) if auth isn't configured yet - callers must
    check _auth_configured() themselves before relying on st.login/st.user
    actually working.
    """
    if not _auth_configured():
        return False
    env = _auth_env()
    secrets_dir = os.path.join(os.path.dirname(__file__), ".streamlit")
    secrets_path = os.path.join(secrets_dir, "secrets.toml")
    os.makedirs(secrets_dir, exist_ok=True)
    # Escape backslashes/quotes defensively even though none of these
    # values are expected to contain TOML-special characters in practice.
    def _esc(v):
        return v.replace("\\", "\\\\").replace('"', '\\"')
    content = (
        "[auth]\n"
        f'redirect_uri = "{_esc(env["redirect_uri"])}"\n'
        f'cookie_secret = "{_esc(env["cookie_secret"])}"\n'
        f'client_id = "{_esc(env["client_id"])}"\n'
        f'client_secret = "{_esc(env["client_secret"])}"\n'
        'server_metadata_url = "https://accounts.google.com/.well-known/openid-configuration"\n'
    )
    try:
        with open(secrets_path, "w") as f:
            f.write(content)
        return True
    except OSError:
        return False


# -----------------------------------
# LOGIN STATE - wrapped defensively since st.user behaves differently
# depending on whether [auth] secrets exist at all.
# -----------------------------------

def is_logged_in():
    # Email-code sessions (email_auth) count exactly like Google ones.
    if st.session_state.get("email_user"):
        return True
    if not _auth_configured():
        return False
    try:
        return bool(st.user.is_logged_in)
    except Exception:
        return False


def current_user_email():
    _em = st.session_state.get("email_user")
    if _em:
        return _em
    if not is_logged_in():
        return None
    try:
        return st.user.email
    except Exception:
        return None


def current_user_name():
    """
    Display name for the account bar - falls back to email if Google didn't
    return a name (or auth isn't configured), so callers never have to
    special-case a blank value themselves.
    """
    _em = st.session_state.get("email_user")
    if _em:
        return _em.split("@")[0]
    if not is_logged_in():
        return None
    try:
        name = getattr(st.user, "name", None)
        if name and name.strip():
            return name.strip()
    except Exception:
        pass
    return current_user_email()


# -----------------------------------
# EMAIL-CODE SIGN-IN (email_auth.py) - session restore, cookie plumbing,
# and the Sign In popover that offers both Google and email.
# -----------------------------------

def _email_auth_available():
    try:
        return email_auth.is_configured()
    except Exception:
        return False


def _client_ip():
    """Best-effort visitor IP for email_auth's per-IP send rate limit
    (see email_auth.MAX_SENDS_PER_IP_PER_DAY). Railway terminates TLS and
    proxies every request, setting X-Forwarded-For to "<client>, <hop>,
    ..." - the first entry is the original visitor. Fails open (returns
    None, which email_auth treats as "skip the per-IP check") on anything
    unexpected rather than ever blocking a real sign-in over a header
    parsing issue."""
    try:
        xff = st.context.headers.get("X-Forwarded-For", "")
        ip = xff.split(",")[0].strip()
        return ip or None
    except Exception:
        return None


def write_auth_cookie(token):
    """Write (token) or clear (token=None) the sdd_auth cookie. ONLY call
    this at the very top of a run (app.py's pending-flag flush) - a call
    immediately followed by st.rerun() never reaches the browser (same
    lesson as the RC view cookie).

    Audit fix (3.2): this used to write the cookie directly via
    `document.cookie=...`, which meant it had no HttpOnly flag and was
    fully readable (and stealable) by any JS on the page - a real risk
    given this token alone gates a signed-in session for 90 days. Fixed
    by having the browser fetch() a same-origin server.py endpoint
    instead (see server.py's /_auth/set-cookie, /_auth/clear-cookie and
    their shared docstring) - THAT process controls a real HTTP response
    and can set Set-Cookie with HttpOnly/Secure/SameSite properly, which
    nothing running inside Streamlit itself can do. The token's own
    generation/validation in email_auth.py is unchanged; only how it
    lands in the browser changes."""
    import streamlit.components.v1 as _components
    if token:
        _js = (
            "fetch('/_auth/set-cookie', {method: 'POST', credentials: 'same-origin', "
            "headers: {'Content-Type': 'application/json'}, "
            f"body: JSON.stringify({{tok: {json.dumps(token)}}})}});"
        )
    else:
        _js = "fetch('/_auth/clear-cookie', {method: 'POST', credentials: 'same-origin'});"
    _components.html(f"<script>{_js}</script>", height=0)


@st.cache_resource(show_spinner=False)
def _run_auth_cleanup_once():
    """email_auth.cleanup() (expired auth_codes/auth_sessions rows) run
    exactly ONCE per server process, not once per visitor session -
    @st.cache_resource (unlike @st.cache_data) shares its cached return
    value across every session on this process, so the first call anywhere
    does the sweep and every later call this boot just returns the cached
    True with no extra DB work. Wrapped in try/except - housekeeping must
    never break sign-in."""
    try:
        email_auth.cleanup()
    except Exception:
        pass
    return True


def restore_email_session():
    """Called once per run from app.py before any page renders: picks the
    email session back up from the sdd_auth cookie after a full page load,
    and records one sign-in per browser session in the aggregate stats
    (works for Google sign-ins too)."""
    _run_auth_cleanup_once()
    if (not st.session_state.get("email_user")
            and not st.session_state.get("email_signed_out")):
        try:
            _tok = st.context.cookies.get("sdd_auth")
        except Exception:
            _tok = None
        if _tok:
            try:
                _em = email_auth.session_email(_tok)
            except Exception:
                _em = None
            if _em:
                st.session_state["email_user"] = _em
                st.session_state["email_auth_token"] = _tok
    if not st.session_state.get("_signup_recorded"):
        _em = current_user_email()
        if _em:
            try:
                email_auth.record_signup(
                    _em,
                    "email" if st.session_state.get("email_user") else "google",
                    src=st.session_state.get("first_src"),
                )
            except Exception:
                pass
            st.session_state["_signup_recorded"] = True


def _sign_out():
    """One Sign out for both auth methods."""
    if st.session_state.get("email_user"):
        try:
            email_auth.revoke(st.session_state.get("email_auth_token"))
        except Exception:
            pass
        st.session_state.pop("email_user", None)
        st.session_state.pop("email_auth_token", None)
        st.session_state.pop("_signup_recorded", None)
        # Same pattern as the RC view exit: block the cookie from
        # re-restoring the session this run, clear it on the next one.
        st.session_state["email_signed_out"] = True
        st.session_state["_pending_auth_cookie_clear"] = True
        st.rerun()
    else:
        st.logout()


def _render_signin_control(key="account_bar_signin", lang="en"):
    """The Sign In control. With email sign-in configured it's a popover
    offering "Continue with Google" and an email-code flow; without it,
    the original plain Google button. The popover key CONTAINS the plain
    button's key, so the existing pill CSS styles both identically.

    lang: only the "Sign In" label itself is translated (Español
    instruction, Part 1) - the popover's own internal copy ("Continue
    with Google", the email/code flow strings below) stays English in
    this pass, an explicitly deferred gap (see i18n.py's docstring)."""
    import i18n
    _label = i18n.t("account.sign_in", lang)
    if not _email_auth_available():
        st.button(_label, key=key, on_click=st.login)
        return
    with st.popover(_label, key=f"{key}_pop"):
        if _auth_configured():
            if st.button("Continue with Google", key=f"{key}_google",
                         type="primary", use_container_width=True):
                st.login()
            st.markdown(
                "<div style='text-align:center;color:#5b7290;font-size:12px;"
                "margin:2px 0 6px;'>&mdash; or &mdash;</div>",
                unsafe_allow_html=True,
            )
        _em_input = st.text_input(
            "Email address", key=f"{key}_email",
            placeholder="you@example.com",
        )
        # Honeypot: a field real visitors never see or fill in (label
        # collapsed, styled off-screen below), but a scripted bot filling
        # every input on the form will populate. A non-empty value here
        # means "not a human" - silently pretend success without ever
        # calling email_auth.send_code(), so no Mailgun quota is spent and
        # the bot gets no signal to distinguish this from a real send.
        st.markdown(
            "<div style='position:absolute;left:-9999px;width:1px;height:1px;"
            "overflow:hidden;' aria-hidden='true'>",
            unsafe_allow_html=True,
        )
        _hp = st.text_input("Website", key=f"{key}_hp", label_visibility="collapsed")
        st.markdown("</div>", unsafe_allow_html=True)
        if not st.session_state.get(f"{key}_code_sent"):
            if st.button("Email me a sign-in code", key=f"{key}_send",
                         use_container_width=True):
                if _hp:
                    st.session_state[f"{key}_code_sent"] = True
                    st.session_state[f"{key}_sent_to"] = _em_input.strip().lower()
                    st.success(f"Code sent to {_em_input.strip()} - check your inbox (and spam folder).")
                else:
                    _ok, _msg = email_auth.send_code(_em_input, client_ip=_client_ip())
                    if _ok:
                        st.session_state[f"{key}_code_sent"] = True
                        st.session_state[f"{key}_sent_to"] = _em_input.strip().lower()
                        st.success(_msg)
                    else:
                        st.error(_msg)
        if st.session_state.get(f"{key}_code_sent"):
            _sent_to = st.session_state.get(f"{key}_sent_to", "")
            _code = st.text_input(
                "6-digit code", key=f"{key}_code", max_chars=6,
                placeholder="123456",
            )
            _cv, _cr = st.columns(2)
            with _cv:
                if st.button("Verify", key=f"{key}_verify", type="primary",
                             use_container_width=True):
                    _tok, _msg = email_auth.verify_code(
                        _sent_to, _code, src=st.session_state.get("first_src")
                    )
                    if _tok:
                        st.session_state["email_user"] = _sent_to
                        st.session_state["email_auth_token"] = _tok
                        st.session_state.pop("email_signed_out", None)
                        st.session_state["_pending_auth_cookie"] = _tok
                        # verify_code already recorded the sign-up.
                        st.session_state["_signup_recorded"] = True
                        st.session_state.pop(f"{key}_code_sent", None)
                        st.rerun()
                    else:
                        st.error(_msg)
            with _cr:
                if st.button("Resend code", key=f"{key}_resend",
                             use_container_width=True):
                    _ok, _msg = email_auth.send_code(_sent_to, client_ip=_client_ip())
                    (st.success if _ok else st.error)(_msg)


# -----------------------------------
# STRIPE SUBSCRIPTION CHECK
# -----------------------------------

@st.cache_data(ttl=3600, show_spinner=False)
def is_subscribed(email):
    """
    True only if `email` has an active Stripe subscription on the
    configured price. Cached per email for an hour - see module docstring
    for why this polls Stripe instead of using a webhook, and why this
    fails CLOSED (returns False) on any error, unlike every other optional
    integration in this app.
    """
    if not email or not _stripe_configured():
        return False
    try:
        import stripe
        stripe.api_key = os.environ["STRIPE_SECRET_KEY"]
        price_id = os.environ["STRIPE_PRICE_ID"]

        customers = stripe.Customer.list(email=email, limit=5)
        for customer in customers.auto_paging_iter():
            subs = stripe.Subscription.list(customer=customer.id, status="active", limit=10)
            for sub in subs.auto_paging_iter():
                for item in sub.get("items", {}).get("data", []):
                    if item.get("price", {}).get("id") == price_id:
                        return True
        return False
    except Exception:
        return False


def create_checkout_url(email):
    """
    Stripe Checkout Session URL for the configured monthly price, or None
    if Stripe isn't configured or the API call fails - callers should show
    a "try again shortly" message rather than a broken link in that case.
    """
    if not email or not _stripe_configured():
        return None
    try:
        import stripe
        stripe.api_key = os.environ["STRIPE_SECRET_KEY"]
        price_id = os.environ["STRIPE_PRICE_ID"]
        redirect_uri = _auth_env()["redirect_uri"]
        base_url = redirect_uri.rsplit("/oauth2callback", 1)[0] or _DEFAULT_REDIRECT_URI.rsplit(
            "/oauth2callback", 1
        )[0]
        session = stripe.checkout.Session.create(
            mode="subscription",
            line_items=[{"price": price_id, "quantity": 1}],
            customer_email=email,
            success_url=f"{base_url}/?checkout=success",
            cancel_url=f"{base_url}/?checkout=cancelled",
        )
        return session.url
    except Exception:
        return None


# -----------------------------------
# SHARED STYLING - reuses the site's own teal (#2dd4bf / hover #14b8a6)
# button theme from app.py's site-wide button CSS, instead of inventing a
# separate dark/navy color for the paywall - so Subscribe/Sign In/Sign out
# and the locked-content box all look like they belong to this site rather
# than a bolted-on module. Targets elements by a substring of their
# Streamlit-generated key class (st-key-<key>) rather than an exact class
# name, since render_gate()'s keys vary per surface (key_prefix differs on
# Deep Dive/Comparison/Research) while still all starting with
# "pw_subscribe_"/"pw_login_"/"pw_logout_"/"pw_gate_box_".
# -----------------------------------

_PILL_BUTTON_CSS = """
<style>
[class*="st-key-account_bar_subscribe"] button,
[class*="st-key-account_bar_subscribe"] a,
[class*="st-key-pw_subscribe_"] button,
[class*="st-key-pw_subscribe_"] a {
    background-color: #2dd4bf !important;
    color: #ffffff !important;
    border: 1.5px solid #2dd4bf !important;
    border-radius: 999px !important;
    padding: 6px 20px !important;
    font-weight: 600 !important;
    font-size: 14px !important;
    box-shadow: none !important;
}
[class*="st-key-account_bar_subscribe"] button:hover,
[class*="st-key-account_bar_subscribe"] a:hover,
[class*="st-key-pw_subscribe_"] button:hover,
[class*="st-key-pw_subscribe_"] a:hover {
    background-color: #14b8a6 !important;
    border-color: #14b8a6 !important;
    color: #ffffff !important;
}
[class*="st-key-account_bar_signin"] button,
[class*="st-key-pw_login_"] button {
    background-color: rgba(45, 212, 191, 0.07) !important;
    color: #2dd4bf !important;
    border: 1.5px solid #2dd4bf !important;
    border-radius: 999px !important;
    padding: 6px 18px !important;
    font-weight: 600 !important;
    font-size: 14px !important;
    box-shadow: none !important;
}
[class*="st-key-account_bar_signin"] button:hover,
[class*="st-key-pw_login_"] button:hover {
    background-color: #2dd4bf !important;
    color: #ffffff !important;
    border-color: #2dd4bf !important;
}
[class*="st-key-account_bar_signout"] button,
[class*="st-key-pw_logout_"] button {
    background-color: transparent !important;
    color: #2dd4bf !important;
    border: 1px solid transparent !important;
    border-radius: 999px !important;
    font-size: 13px !important;
    padding: 4px 12px !important;
    box-shadow: none !important;
}
[class*="st-key-account_bar_signout"] button:hover,
[class*="st-key-pw_logout_"] button:hover {
    background-color: #2dd4bf !important;
    color: #ffffff !important;
    border-color: #2dd4bf !important;
}
[class*="st-key-pw_gate_box_"] {
    border-color: #14b8a6 !important;
    background-color: #10312d !important;
    border-radius: 12px !important;
}
.pw-account-name {
    display: flex;
    align-items: center;
    justify-content: flex-start;
    height: 38px;
    font-size: 14px;
    font-weight: 600;
    color: #e6edf5;
    font-family: 'Segoe UI', sans-serif;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
}
</style>
"""


# -----------------------------------
# ACCOUNT BAR - slim strip shown above the site header on every page
# (called from app.py's _render_header()). Separate from render_gate:
# this is always-visible identity/sign-in chrome, while render_gate blocks
# one specific section's content. Renders nothing at all when the paywall
# is off or Google sign-in isn't configured yet, so pages look exactly as
# they did before this existed until Andrew flips PAYWALL_ENABLED and
# finishes setup - same dormant-by-default rule as the rest of this module.
# -----------------------------------

def _right_widget_columns(left_widths, extra_widget=None, extra_widget2=None,
                          trailing_width=None, total=12.0, extra_widget3=None):
    """
    Shared column plumbing for the account bar rows: the given left-edge
    widths, then a flexible spacer, then one dedicated column per provided
    extra widget (2.6 for the feedback popover, 1.4 for the compact second
    widget, 1.0 for the compact third/language-picker widget), then an
    optional trailing column (Sign out). Each widget gets its OWN column,
    so none of them can ever overlap each other.
    Returns (left_cols, widget_cols, trailing_col_or_None).
    """
    _rw = (([2.6] if extra_widget else []) + ([1.4] if extra_widget2 else [])
           + ([1.0] if extra_widget3 else []))
    _trail = [trailing_width] if trailing_width else []
    _spacer = max(0.5, total - sum(left_widths) - sum(_rw) - sum(_trail))
    cols = st.columns(left_widths + [_spacer] + _rw + _trail, gap="small")
    _n_left = len(left_widths)
    _widget_cols = []
    _i = _n_left + 1
    for _w in (extra_widget, extra_widget2, extra_widget3):
        if _w:
            _widget_cols.append((cols[_i], _w))
            _i += 1
    return cols[:_n_left], _widget_cols, (cols[-1] if trailing_width else None)


def _render_name_and_signout(name, extra_widget=None, extra_widget2=None,
                             extra_widget3=None, lang="en"):
    """
    Shared layout for "signed in, nothing else to show but Sign out" -
    used both when the paywall is off entirely and when a subscriber
    already has an active subscription, so those two cases render
    identically instead of drifting apart over time.

    extra_widget / extra_widget2 / extra_widget3: optional zero-arg
        callables (e.g. the page's feedback button, the RC view unlock,
        and the EN/ES language picker) each rendered in their OWN column
        immediately to the left of Sign out.
    lang: "en"/"es" - translates just the Sign out label (Español
        instruction, Part 1).
    """
    import i18n
    _left, _widgets, _c3 = _right_widget_columns(
        [1.4], extra_widget, extra_widget2, trailing_width=1.1,
        extra_widget3=extra_widget3)
    with _left[0]:
        st.markdown(
            f'<div class="pw-account-name">{html.escape(name or "")}</div>',
            unsafe_allow_html=True,
        )
    for _col, _w in _widgets:
        with _col:
            _w()
    with _c3:
        if st.button(i18n.t("account.sign_out", lang), key="account_bar_signout"):
            _sign_out()


def render_account_bar(extra_widget=None, extra_widget2=None, extra_widget3=None,
                       lang="en"):
    """
    Sign In (or the signed-in name + Sign out) shows whenever Google
    sign-in is configured, regardless of PAYWALL_ENABLED - Andrew can start
    building an account base and receiving feedback attributed to a real
    signed-in email well before subscriptions themselves ever turn on.
    Subscribe only ever appears - and only ever calls Stripe - when
    PAYWALL_ENABLED is ALSO true; with it off, this is sign-in chrome only.

    extra_widget / extra_widget2 / extra_widget3: optional zero-arg
        callables rendered in the same row as Sign out (or right-aligned on
        their own when Sign out isn't shown), each in its OWN column - used
        for the page's feedback button, the RC view unlock, and the EN/ES
        language picker.
    lang: "en"/"es" - Español instruction, Part 1. Translates the Sign In/
        Sign out/Subscribe labels in this bar; the email sign-in popover's
        own internal copy stays English (deferred, see i18n.py).
    """
    import i18n
    if not _auth_configured() and not _email_auth_available():
        # No sign-in method configured at all - no account bar, but the
        # widgets (if any) still need to render somewhere - same
        # standalone right-aligned row they used to have on their own.
        if extra_widget or extra_widget2 or extra_widget3:
            _left, _widgets, _ = _right_widget_columns(
                [0.5], extra_widget, extra_widget2, extra_widget3=extra_widget3)
            for _col, _w in _widgets:
                with _col:
                    _w()
        return

    _ensure_auth_secrets_written()
    st.markdown(_PILL_BUTTON_CSS, unsafe_allow_html=True)

    # Layout: identity + Subscribe (when shown) hug the left edge (first
    # thing a visitor sees), the widgets and Sign out hug the right edge -
    # an empty spacer column in between pushes the two groups apart to the
    # full width of the page rather than bunching everything together.
    if not is_logged_in():
        if not PAYWALL_ENABLED:
            _left, _widgets, _ = _right_widget_columns(
                [1.2], extra_widget, extra_widget2, extra_widget3=extra_widget3)
            with _left[0]:
                _render_signin_control("account_bar_signin", lang=lang)
            for _col, _w in _widgets:
                with _col:
                    _w()
            return

        _left, _widgets, _ = _right_widget_columns(
            [1.2, 1.0], extra_widget, extra_widget2, extra_widget3=extra_widget3)
        with _left[0]:
            _render_signin_control("account_bar_signin", lang=lang)
        with _left[1]:
            # Not logged in yet, so we don't have an email for Stripe -
            # route through the same sign-in first; once they're back,
            # this becomes the real Subscribe button below.
            st.button(i18n.t("account.subscribe", lang), key="account_bar_subscribe", on_click=st.login)
        for _col, _w in _widgets:
            with _col:
                _w()
        return

    name = current_user_name()

    if not PAYWALL_ENABLED:
        _render_name_and_signout(name, extra_widget=extra_widget,
                                 extra_widget2=extra_widget2,
                                 extra_widget3=extra_widget3, lang=lang)
        return

    email = current_user_email()
    if is_subscribed(email):
        _render_name_and_signout(name, extra_widget=extra_widget,
                                 extra_widget2=extra_widget2,
                                 extra_widget3=extra_widget3, lang=lang)
        return

    _left, _widgets, _c3 = _right_widget_columns(
        [1.2, 1.0], extra_widget, extra_widget2, trailing_width=1.1,
        extra_widget3=extra_widget3)
    with _left[0]:
        st.markdown(
            f'<div class="pw-account-name">{html.escape(name or "")}</div>',
            unsafe_allow_html=True,
        )
    with _left[1]:
        _subscribe_label = i18n.t("account.subscribe", lang)
        if _stripe_configured():
            checkout_url = create_checkout_url(email)
            if checkout_url:
                st.link_button(_subscribe_label, checkout_url, key="account_bar_subscribe")
            else:
                st.button(_subscribe_label, key="account_bar_subscribe_err", disabled=True)
        else:
            st.button(_subscribe_label, key="account_bar_subscribe_soon", disabled=True)
    for _col, _w in _widgets:
        with _col:
            _w()
    with _c3:
        st.button(i18n.t("account.sign_out", lang), key="account_bar_signout", on_click=st.logout)


# -----------------------------------
# THE GATE - the one function gated pages/sections call.
# -----------------------------------

def render_gate(feature_label, teaser=None, key_prefix="", lang="en"):
    """
    Call this immediately before rendering a premium section. Returns True
    if the caller should render the real content (paywall off, or a
    confirmed active subscriber); returns False if this already rendered a
    login/subscribe prompt in its place, in which case the caller should
    skip rendering that section.

    feature_label: short name shown in the prompt, e.g. "the full
        Comparison table" or "Company Potential".
    teaser: optional one-line description of what's behind the lock.
    key_prefix: unique-ish prefix for Streamlit widget keys, needed when
        render_gate() is called more than once on the same page (e.g. once
        for Company Potential, once for Fair Value on the Research page).
    lang: "en" (default) or "es" - Español instruction, Part 1. Only this
        function's OWN fixed chrome (the lock title, Subscribe/Sign-in
        button labels, the "signed in as" line) is routed through i18n;
        feature_label/teaser stay whatever the caller passed in, since
        translating every call site's custom copy is out of scope for
        this pass (see i18n.py's module docstring for the full list).
    """
    import i18n
    if not PAYWALL_ENABLED:
        return True

    _ensure_auth_secrets_written()
    st.markdown(_PILL_BUTTON_CSS, unsafe_allow_html=True)

    box = st.container(border=True, key=f"pw_gate_box_{key_prefix}")

    if (not _auth_configured() and not _email_auth_available()) \
            or not _stripe_configured():
        with box:
            st.info(i18n.t("gate.not_configured", lang, feature_label=feature_label))
        return False

    if not is_logged_in():
        with box:
            st.markdown(f"**{i18n.t('gate.locked_title', lang, feature_label=feature_label)}**")
            if teaser:
                st.caption(teaser)
            if _email_auth_available():
                _render_signin_control(f"pw_login_{key_prefix}")
            else:
                st.button(
                    i18n.t("gate.google_signin", lang),
                    key=f"pw_login_{key_prefix}",
                    on_click=st.login,
                    type="primary",
                )
        return False

    email = current_user_email()
    if is_subscribed(email):
        return True

    with box:
        st.markdown(f"**{i18n.t('gate.locked_title', lang, feature_label=feature_label)}**")
        if teaser:
            st.caption(teaser)
        checkout_url = create_checkout_url(email)
        if checkout_url:
            st.link_button(
                i18n.t("gate.subscribe_cta", lang),
                checkout_url,
                type="primary",
                key=f"pw_subscribe_{key_prefix}",
            )
        else:
            st.warning(i18n.t("gate.checkout_error", lang))
        st.caption(i18n.t("gate.signed_in_as", lang, email=email))
        if st.button(i18n.t("account.sign_out", lang), key=f"pw_logout_{key_prefix}"):
            _sign_out()
    return False
