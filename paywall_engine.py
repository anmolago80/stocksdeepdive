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
import os

import streamlit as st


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
    if not _auth_configured():
        return False
    try:
        return bool(st.user.is_logged_in)
    except Exception:
        return False


def current_user_email():
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

def _render_name_and_signout(name, extra_widget=None):
    """
    Shared layout for "signed in, nothing else to show but Sign out" -
    used both when the paywall is off entirely and when a subscriber
    already has an active subscription, so those two cases render
    identically instead of drifting apart over time.

    extra_widget: optional zero-arg callable (e.g. the page's feedback
        button) rendered in its own column immediately to the left of
        Sign out, so it sits "beside" it in the same row instead of on a
        separate row above. Only reserves that column when actually
        provided, so pages that don't pass one (e.g. Home) keep the
        original two-group spacing.
    """
    if extra_widget:
        _c1, _sp, _c2, _c3 = st.columns([1.4, 8.6, 1.6, 1.0], gap="small")
    else:
        _c1, _sp, _c3 = st.columns([1.4, 9.6, 1.0], gap="small")
        _c2 = None
    with _c1:
        st.markdown(
            f'<div class="pw-account-name">{html.escape(name or "")}</div>',
            unsafe_allow_html=True,
        )
    if extra_widget and _c2 is not None:
        with _c2:
            extra_widget()
    with _c3:
        st.button("Sign out", key="account_bar_signout", on_click=st.logout)


def render_account_bar(extra_widget=None):
    """
    Sign In (or the signed-in name + Sign out) shows whenever Google
    sign-in is configured, regardless of PAYWALL_ENABLED - Andrew can start
    building an account base and receiving feedback attributed to a real
    signed-in email well before subscriptions themselves ever turn on.
    Subscribe only ever appears - and only ever calls Stripe - when
    PAYWALL_ENABLED is ALSO true; with it off, this is sign-in chrome only.

    extra_widget: optional zero-arg callable rendered in the same row as
        Sign out (or, when signed out and Sign out isn't shown at all,
        right-aligned on its own) - used to embed the page's feedback
        button in this bar instead of it sitting on a separate row above.
    """
    if not _auth_configured():
        # No account bar at all in this case, but the feedback button
        # (if any) still needs to render somewhere - same standalone
        # right-aligned row it used to have on its own.
        if extra_widget:
            _sp, _c1 = st.columns([10.3, 1.7], gap="small")
            with _c1:
                extra_widget()
        return

    _ensure_auth_secrets_written()
    st.markdown(_PILL_BUTTON_CSS, unsafe_allow_html=True)

    # Layout: identity + Subscribe (when shown) hug the left edge (first
    # thing a visitor sees), Sign out (and the feedback button beside it,
    # when present) hugs the right edge - an empty spacer column in
    # between pushes the two groups apart to the full width of the page
    # rather than bunching everything together.
    if not is_logged_in():
        if not PAYWALL_ENABLED:
            if extra_widget:
                _c1, _sp, _c2 = st.columns([1.0, 9.5, 1.5], gap="small")
                with _c2:
                    extra_widget()
            else:
                _c1, _sp = st.columns([1.0, 11.0], gap="small")
            with _c1:
                st.button("Sign In", key="account_bar_signin", on_click=st.login)
            return

        if extra_widget:
            _c1, _c2, _sp, _c3 = st.columns([1.0, 1.0, 8.5, 1.5], gap="small")
            with _c3:
                extra_widget()
        else:
            _c1, _c2, _sp = st.columns([1.0, 1.0, 10.0], gap="small")
        with _c1:
            st.button("Sign In", key="account_bar_signin", on_click=st.login)
        with _c2:
            # Not logged in yet, so we don't have an email for Stripe -
            # route through the same sign-in first; once they're back,
            # this becomes the real Subscribe button below.
            st.button("Subscribe", key="account_bar_subscribe", on_click=st.login)
        return

    name = current_user_name()

    if not PAYWALL_ENABLED:
        _render_name_and_signout(name, extra_widget=extra_widget)
        return

    email = current_user_email()
    if is_subscribed(email):
        _render_name_and_signout(name, extra_widget=extra_widget)
        return

    if extra_widget:
        _c1, _c2, _sp, _c4, _c3 = st.columns([1.2, 1.0, 6.8, 1.6, 1.0], gap="small")
    else:
        _c1, _c2, _sp, _c3 = st.columns([1.2, 1.0, 8.8, 1.0], gap="small")
        _c4 = None
    with _c1:
        st.markdown(
            f'<div class="pw-account-name">{html.escape(name or "")}</div>',
            unsafe_allow_html=True,
        )
    with _c2:
        if _stripe_configured():
            checkout_url = create_checkout_url(email)
            if checkout_url:
                st.link_button("Subscribe", checkout_url, key="account_bar_subscribe")
            else:
                st.button("Subscribe", key="account_bar_subscribe_err", disabled=True)
        else:
            st.button("Subscribe", key="account_bar_subscribe_soon", disabled=True)
    if extra_widget and _c4 is not None:
        with _c4:
            extra_widget()
    with _c3:
        st.button("Sign out", key="account_bar_signout", on_click=st.logout)


# -----------------------------------
# THE GATE - the one function gated pages/sections call.
# -----------------------------------

def render_gate(feature_label, teaser=None, key_prefix=""):
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
    """
    if not PAYWALL_ENABLED:
        return True

    _ensure_auth_secrets_written()
    st.markdown(_PILL_BUTTON_CSS, unsafe_allow_html=True)

    box = st.container(border=True, key=f"pw_gate_box_{key_prefix}")

    if not _auth_configured() or not _stripe_configured():
        with box:
            st.info(
                f"🔒 Subscriptions aren't fully set up yet - {feature_label} will "
                "unlock here once they are. Check back soon."
            )
        return False

    if not is_logged_in():
        with box:
            st.markdown(f"**🔒 Subscribe to unlock {feature_label}**")
            if teaser:
                st.caption(teaser)
            st.button(
                "Sign in with Google to continue",
                key=f"pw_login_{key_prefix}",
                on_click=st.login,
                type="primary",
            )
        return False

    email = current_user_email()
    if is_subscribed(email):
        return True

    with box:
        st.markdown(f"**🔒 Subscribe to unlock {feature_label}**")
        if teaser:
            st.caption(teaser)
        checkout_url = create_checkout_url(email)
        if checkout_url:
            st.link_button(
                "Subscribe to continue browsing",
                checkout_url,
                type="primary",
                key=f"pw_subscribe_{key_prefix}",
            )
        else:
            st.warning(
                "Couldn't reach the subscription system just now - please try "
                "again in a moment."
            )
        st.caption(f"Signed in as {email}.")
        st.button("Sign out", key=f"pw_logout_{key_prefix}", on_click=st.logout)
    return False
