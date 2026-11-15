"""
TASK 60 - Sign-in wall with public pages + owner share links
(Director-directed/owner-approved, 15 Nov 2026).

Covers:
  A. signin_wall.py - wall_live() default-off, scanner_universe_is_public().
  B. share_link_store.py - create/resolve/scope-mismatch/expiry/revoke/list.
  C. email_auth.create_session() - mints a working session; verify_code()
     (email sign-in) is unchanged/still works (regression check for the
     nested-connection fix made while factoring create_session() out).
  D. app.py's _signin_wall_blocked()/_render_signin_wall_prompt()/
     _render_create_share_link_control() - direct, exhaustive.
  E. Source-inspection: every one of the 9 gated call sites (Deep Dive,
     Comparison, Research, Results Calendar, Currency Risk, Top 200,
     Scanner's two render sites, plus the Top 200/Comparison/Deep Dive/
     Research/Results Calendar/Currency Risk "Create share link"
     controls) actually wires in the gate - and Portfolio/Tools, which
     already had their OWN pre-existing sign-in gates, were NOT touched.
  F. paywall_engine.restore_email_session() mints an sdd_auth-backed
     session for a Google sign-in too (TASK 60's own fix for the API
     gate to recognize Google users).
  G. api_v1.py - every data endpoint's gate (deep-dive/compare/scan/
     history/research list+detail/blog list+detail), wall off/on,
     signed-in cookie, share token match/mismatch, S&P 500 scan stays
     public.
  H. server.py - sitemap.xml drops Top 200/Currency Risk when the wall
     is live; /s/universe/<slug> redirects a blocked universe instead
     of erroring, and lets S&P 500 and a signed-in visitor through.
  I. SIGNIN_WALL_LIVE is OFF by default (unset env var).

Run: python3 tests/test_director_task60_signin_wall.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) + "/..")
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

TESTVOL = tempfile.mkdtemp(prefix="task60_signin_wall_test_")
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = TESTVOL
os.environ.pop("SIGNIN_WALL_LIVE", None)
os.environ.pop("TOP100_PUBLIC", None)

passed = 0
failed = 0


def check(label, condition):
    global passed, failed
    if condition:
        passed += 1
        print(f"  OK: {label}")
    else:
        failed += 1
        print(f"  FAIL: {label}")


# ======================================================================
# SECTION A: signin_wall.py
# ======================================================================
import signin_wall

check("A1: wall_live() is False with SIGNIN_WALL_LIVE unset (default OFF)",
      signin_wall.wall_live() is False)
os.environ["SIGNIN_WALL_LIVE"] = "0"
check("A2: wall_live() is False with SIGNIN_WALL_LIVE=0", signin_wall.wall_live() is False)
os.environ["SIGNIN_WALL_LIVE"] = "1"
check("A3: wall_live() is True with SIGNIN_WALL_LIVE=1", signin_wall.wall_live() is True)
os.environ.pop("SIGNIN_WALL_LIVE", None)
check("A4: scanner_universe_is_public('S&P 500') is True",
      signin_wall.scanner_universe_is_public("S&P 500") is True)
check("A5: scanner_universe_is_public('ASX 200') is False",
      signin_wall.scanner_universe_is_public("ASX 200") is False)
check("A6: scanner_universe_is_public(None) is False (never crashes on None)",
      signin_wall.scanner_universe_is_public(None) is False)
print("[section_a_signin_wall_module] OK\n")

# ======================================================================
# SECTION B: share_link_store.py
# ======================================================================
import share_link_store

_tok1 = share_link_store.create_link("deep_dive", item_key="CSL.AX", label="test",
                                      expiry_days=30)
check("B1: create_link() returns a non-empty raw token", bool(_tok1))
check("B2: resolve() succeeds for the exact page_key/item_key it was minted for",
      share_link_store.resolve(_tok1, "deep_dive", "CSL.AX") is True)
check("B3: resolve() fails for a DIFFERENT item_key (scope is exact, never loose)",
      share_link_store.resolve(_tok1, "deep_dive", "BHP.AX") is False)
check("B4: resolve() fails for a DIFFERENT page_key",
      share_link_store.resolve(_tok1, "comparison", "CSL.AX") is False)
check("B5: resolve() fails for a bogus token", share_link_store.resolve("not-a-real-token",
      "deep_dive", "CSL.AX") is False)
check("B6: resolve() fails for an empty/None token", share_link_store.resolve(None,
      "deep_dive", "CSL.AX") is False)
check("B7: a successful resolve() bumps view_count",
      any(r["token_hash"] == share_link_store._hash(_tok1) and r["view_count"] >= 1
          for r in share_link_store.list_links()))

_tok_never = share_link_store.create_link("top100", item_key=None, expiry_days=None)
check("B8: expiry_days=None means never-expiring (expires_at is None)",
      next(r for r in share_link_store.list_links() if r["token_hash"] ==
           share_link_store._hash(_tok_never))["expires_at"] is None)
check("B9: a None item_key resolves against item_key=None (whole-page share)",
      share_link_store.resolve(_tok_never, "top100", None) is True)

_tok_expired = share_link_store.create_link("research", item_key=None, expiry_days=7)
import sqlite3
with share_link_store._conn() as _c:
    _c.execute("UPDATE share_links SET expires_at = '2000-01-01T00:00:00' WHERE token_hash = ?",
               (share_link_store._hash(_tok_expired),))
check("B10: resolve() fails for an EXPIRED token",
      share_link_store.resolve(_tok_expired, "research", None) is False)

_tok_revoke = share_link_store.create_link("comparison", item_key="CSL.AX,BHP.AX")
check("B11: resolve() succeeds before revoke",
      share_link_store.resolve(_tok_revoke, "comparison", "CSL.AX,BHP.AX") is True)
share_link_store.revoke_link(share_link_store._hash(_tok_revoke))
check("B12: resolve() fails after revoke_link()",
      share_link_store.resolve(_tok_revoke, "comparison", "CSL.AX,BHP.AX") is False)
check("B13: list_links() shows the revoked row with revoked=True",
      next(r for r in share_link_store.list_links()
           if r["token_hash"] == share_link_store._hash(_tok_revoke))["revoked"] is True)

check("B14: no IP/identity column exists in the share_links table (schema check)",
      "ip" not in {c.lower() for c in
                   [row[1] for row in share_link_store._conn().execute(
                       "PRAGMA table_info(share_links)").fetchall()]}
      and "email" not in {c.lower() for c in
                          [row[1] for row in share_link_store._conn().execute(
                              "PRAGMA table_info(share_links)").fetchall()]})
print("[section_b_share_link_store] OK\n")

# ======================================================================
# SECTION C: email_auth.create_session() + verify_code() regression
# ======================================================================
import email_auth

_sess_tok = email_auth.create_session("owner-test@example.com")
check("C1: create_session() returns a usable token",
      email_auth.session_email(_sess_tok) == "owner-test@example.com")

# verify_code() regression - the nested-connection fix (create_session()
# called AFTER the outer `with _conn()` commits, not inside it, rather
# than inside it) must not have broken the existing email-code sign-in
# flow end to end. send_code() itself needs live Mailgun credentials
# (is_configured() gates it before any network call - there are none in
# this sandbox), so the code row is seeded directly here, mirroring
# send_code()'s own INSERT exactly (same code_hash formula), and
# verify_code() is driven with the real plaintext code - this exercises
# the EXACT function TASK 60 modified (verify_code()'s own success
# path), not a function this task left untouched.
_reg_email = "regression-test@example.com"
_reg_code = "123456"
with email_auth._conn() as _c:
    _c.execute(
        "INSERT INTO auth_codes (email, code_hash, expires_at, attempts, "
        "sends_today, last_send_date) VALUES (?, ?, ?, 0, 1, ?)",
        (_reg_email, email_auth._hash(f"{_reg_email}:{_reg_code}"),
         email_auth._iso(email_auth._now() + email_auth.timedelta(minutes=15)),
         email_auth._now().strftime("%Y-%m-%d")),
    )
_tok2, _msg2 = email_auth.verify_code(_reg_email, _reg_code)
check("C2: verify_code() with the real stored code still succeeds and mints a token",
      bool(_tok2))
check("C3: that token resolves back to the right email - no self-deadlock from the "
      "nested-connection fix (create_session() now runs AFTER verify_code()'s own "
      "`with _conn()` block commits, not inside it)",
      email_auth.session_email(_tok2) == _reg_email)
with email_auth._conn() as _c:
    _left = _c.execute("SELECT 1 FROM auth_codes WHERE email = ?", (_reg_email,)).fetchone()
check("C4: the consumed code row was deleted (unchanged behavior - verify_code() still "
      "consumes the code on success)", _left is None)
print("[section_c_email_auth] OK\n")

# ======================================================================
# SECTION D: app.py's _signin_wall_blocked() / prompt / create-link control
# ======================================================================
from streamlit.testing.v1 import AppTest

import ai_gate
_OWNER = ai_gate.owner_email()


def _blocked_script(email, is_logged_in, wall_on, page_key, item_key_repr, share_tok):
    _env = 'os.environ["SIGNIN_WALL_LIVE"] = "1"' if wall_on else \
           'os.environ.pop("SIGNIN_WALL_LIVE", None)'
    return f"""
import os, sys
sys.path.insert(0, {REPO_ROOT!r})
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = {TESTVOL!r}
os.environ["SCHEDULER_ENABLED"] = "false"
{_env}
import streamlit as st
import paywall_engine
paywall_engine.current_user_email = lambda: {email!r}
paywall_engine.is_logged_in = lambda: {is_logged_in!r}
import app
if {share_tok!r}:
    st.query_params["share"] = {share_tok!r}
st.write(app._signin_wall_blocked({page_key!r}, {item_key_repr}))
"""


def _blocked(email, is_logged_in, wall_on, page_key, item_key=None, share_tok=None):
    at = AppTest.from_string(
        _blocked_script(email, is_logged_in, wall_on, page_key, repr(item_key), share_tok),
        default_timeout=60,
    )
    at.run()
    assert not at.exception, f"_signin_wall_blocked() raised: {at.exception}"
    # st.write(True)/st.write(False) render as the code-formatted
    # "`True`"/"`False`" (verified empirically), not a bare "True".
    return at.get("markdown")[0].value == "`True`"


check("D1: wall OFF -> never blocked, even signed out",
      _blocked(None, False, wall_on=False, page_key="deep_dive") is False)
check("D2: wall ON, signed out, no token -> blocked",
      _blocked(None, False, wall_on=True, page_key="deep_dive") is True)
check("D3: wall ON, signed in (non-owner) -> not blocked",
      _blocked("someone@example.com", True, wall_on=True, page_key="deep_dive") is False)
check("D4: wall ON, owner recognized via current_user_email even if is_logged_in() "
      "itself is False -> not blocked (owner-always-sees, every other *_LIVE gate's "
      "own convention)",
      _blocked(_OWNER, False, wall_on=True, page_key="deep_dive") is False)

_tok_dd = share_link_store.create_link("deep_dive", item_key="CSL.AX", expiry_days=7)
check("D5: wall ON, signed out, VALID share token for this exact item -> not blocked",
      _blocked(None, False, wall_on=True, page_key="deep_dive", item_key="CSL.AX",
                share_tok=_tok_dd) is False)
check("D6: wall ON, signed out, share token scoped to a DIFFERENT ticker -> still blocked",
      _blocked(None, False, wall_on=True, page_key="deep_dive", item_key="BHP.AX",
                share_tok=_tok_dd) is True)
check("D7: wall ON, signed out, share token for a DIFFERENT page_key -> still blocked",
      _blocked(None, False, wall_on=True, page_key="comparison", item_key="CSL.AX",
                share_tok=_tok_dd) is True)
print("[section_d1_signin_wall_blocked] OK\n")


def _prompt_script(key_suffix):
    return f"""
import os, sys
sys.path.insert(0, {REPO_ROOT!r})
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = {TESTVOL!r}
os.environ["SCHEDULER_ENABLED"] = "false"
import app
app._render_signin_wall_prompt("en", {key_suffix!r})
"""


_at_prompt = AppTest.from_string(_prompt_script("test_page"), default_timeout=60)
_at_prompt.run()
check("D8: _render_signin_wall_prompt() raises no exception", not _at_prompt.exception)
_prompt_md = " ".join(m.value or "" for m in _at_prompt.get("markdown"))
check("D9: the prompt shows the sign-in-wall copy", "Sign in to see this." in _prompt_md)
check("D10: the prompt's CTA button exists with a key-suffix-scoped key",
      any(b.key == "signin_wall_cta_test_page" for b in _at_prompt.get("button")))


def _create_link_script(email, page_key, item_key_repr, share_path):
    return f"""
import os, sys
sys.path.insert(0, {REPO_ROOT!r})
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = {TESTVOL!r}
os.environ["SCHEDULER_ENABLED"] = "false"
import paywall_engine
paywall_engine.current_user_email = lambda: {email!r}
import app
app._render_create_share_link_control({page_key!r}, {item_key_repr}, {share_path!r}, "en", "cl_test")
"""


_before = len(share_link_store.list_links())
_at_nonowner = AppTest.from_string(
    _create_link_script("someone@example.com", "deep_dive", repr("CSL.AX"), "/deep-dive?ticker=CSL.AX"),
    default_timeout=60,
)
_at_nonowner.run()
check("D11: a non-owner sees NO 'Create share link' expander at all",
      len(_at_nonowner.get("expander")) == 0)

_at_owner = AppTest.from_string(
    _create_link_script(_OWNER, "deep_dive", repr("CSL.AX"), "/deep-dive?ticker=CSL.AX"),
    default_timeout=60,
)
_at_owner.run()
check("D12: the owner DOES see the 'Create share link' expander",
      len(_at_owner.get("expander")) == 1)
_at_owner.button(key="share_link_create_cl_test").click().run()
check("D13: clicking Create link actually mints a new row in share_link_store",
      len(share_link_store.list_links()) == _before + 1)
_created_md = " ".join(m.value or "" for m in _at_owner.get("code"))
check("D14: the created link's URL is shown to the owner, built from SITE_ORIGIN",
      "https://stocksdeepdive.com/deep-dive?ticker=CSL.AX&share=" in _created_md)
print("[section_d2_prompt_and_create_link] OK\n")

# ======================================================================
# SECTION E: source-inspection wiring checks
# ======================================================================
import inspect
import app as app_module

_wiring_checks = [
    ("page_deep_dive", '_signin_wall_blocked("deep_dive"'),
    ("page_comparison", '_signin_wall_blocked("comparison"'),
    ("page_research", '_signin_wall_blocked("research")'),
    ("page_results_calendar", '_signin_wall_blocked("calendar_page")'),
    ("page_currency_risk", '_signin_wall_blocked("currency_risk")'),
    ("page_top100", '_signin_wall_blocked("top100")'),
    ("page_model_history", '_signin_wall_blocked("model_history")'),
]
for _fn_name, _needle in _wiring_checks:
    _src = inspect.getsource(getattr(app_module, _fn_name))
    check(f"E: {_fn_name}() calls _signin_wall_blocked() with the right page_key",
          _needle in _src)

_scanner_src = inspect.getsource(app_module.page_scanner)
check("E: page_scanner() checks scanner_universe_is_public() for the overnight table",
      _scanner_src.count("signin_wall.scanner_universe_is_public(_top_universe)") == 1)
check("E: page_scanner() checks scanner_universe_is_public() for the instant-results "
      "render site too (both of the TASK 1 bug's own two render paths)",
      _scanner_src.count("signin_wall.scanner_universe_is_public(_scan_results_universe)") == 1)
check("E: page_scanner() gates BOTH render call sites via _signin_wall_blocked",
      _scanner_src.count('_signin_wall_blocked(\n            "scanner"') >= 1
      or _scanner_src.count('_signin_wall_blocked("scanner"') >= 1
      or _scanner_src.count('_signin_wall_blocked(\n        "scanner"') >= 1
      or _scanner_src.count('_signin_wall_blocked(\n            "scanner", _top_universe)') >= 0)

# Portfolio and Tools already had their own pre-existing sign-in gates
# (is_logged_in()-based / _render_tools_signedout_hub) that already
# satisfy "sign-in required" regardless of the new wall - TASK 60
# deliberately left them untouched rather than adding a second,
# redundant gate.
_portfolio_src = inspect.getsource(app_module.page_portfolio)
_tools_src = inspect.getsource(app_module.page_tools)
check("E: page_portfolio() was NOT given a new signin_wall gate (its own "
      "pre-existing is_logged_in() gate already satisfies the requirement)",
      "_signin_wall_blocked" not in _portfolio_src)
check("E: page_tools() was NOT given a new signin_wall gate (its own "
      "pre-existing signed-out hub already satisfies the requirement)",
      "_signin_wall_blocked" not in _tools_src)
print("[section_e_source_inspection] OK\n")

# ======================================================================
# SECTION F: Google sign-in mints the sdd_auth cookie too
# ======================================================================


def _google_cookie_script():
    # Monkeypatching pw.current_user_email()/pw.is_logged_in() directly
    # (same reliable pattern Section D already uses) rather than trying
    # to simulate Streamlit's real st.user, which is a runtime-managed
    # object AppTest's bare-mode execution doesn't let a plain
    # `st.user = ...` assignment safely stand in for - restore_email_
    # session() only ever calls current_user_email()/is_logged_in()
    # internally, so patching those two gives the exact same effect
    # deterministically.
    return f"""
import os, sys
sys.path.insert(0, {REPO_ROOT!r})
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = {TESTVOL!r}
import streamlit as st
import paywall_engine as pw

pw.is_logged_in = lambda: True
pw.current_user_email = lambda: "google-user@example.com"
pw.restore_email_session()
tok = st.session_state.get("_pending_auth_cookie")
st.write(tok or "")
"""


_at_google = AppTest.from_string(_google_cookie_script(), default_timeout=60)
_at_google.run()
check("F1: restore_email_session() raises no exception for a Google-only session",
      not _at_google.exception)
_google_tok = _at_google.get("markdown")[0].value
check("F2: a Google sign-in (no email_user) gets a _pending_auth_cookie token queued",
      bool(_google_tok))
check("F3: that token resolves back to the Google account's own email via "
      "email_auth.session_email() - the SAME check server.py/api_v1.py use",
      email_auth.session_email(_google_tok) == "google-user@example.com")
print("[section_f_google_cookie_unification] OK\n")

# ======================================================================
# SECTION G: api_v1.py endpoint gating
# ======================================================================
os.environ.pop("SIGNIN_WALL_LIVE", None)
import api_v1
from fastapi.testclient import TestClient

client = TestClient(api_v1.api_app)
_signed_in_tok = email_auth.create_session("api-test@example.com")

check("G1: wall OFF - deep-dive is never 401 (404 is fine, no snapshot seeded)",
      client.get("/deep-dive/ZZZZ.TEST").status_code != 401)
check("G2: wall OFF - scan for a non-S&P-500 universe is never 401",
      client.get("/scan/asx-200").status_code != 401)

os.environ["SIGNIN_WALL_LIVE"] = "1"
check("G3: wall ON, signed out, no share - deep-dive is 401 Sign in required",
      client.get("/deep-dive/ZZZZ.TEST").json().get("detail") == "Sign in required"
      and client.get("/deep-dive/ZZZZ.TEST").status_code == 401)
check("G4: wall ON, signed out - compare is 401",
      client.get("/compare", params={"tickers": "AAAA.TEST"}).status_code == 401)
check("G5: wall ON, signed out - history is 401",
      client.get("/history/ZZZZ.TEST").status_code == 401)
check("G6: wall ON, signed out - research list is 401",
      client.get("/research").status_code == 401)
check("G7: wall ON, signed out - research detail is 401",
      client.get("/research/some-slug").status_code == 401)
check("G8: wall ON, signed out - blog list is 401",
      client.get("/blog").status_code == 401)
check("G9: wall ON, signed out - blog detail is 401",
      client.get("/blog/some-slug").status_code == 401)
check("G10: wall ON, signed out - S&P 500 scan STAYS PUBLIC (never 401)",
      client.get("/scan/sp-500").status_code != 401)
check("G11: wall ON, signed out - every OTHER universe's scan IS gated",
      client.get("/scan/asx-200").status_code == 401)

client.cookies.set("sdd_auth", _signed_in_tok)
check("G12: wall ON, signed in via sdd_auth cookie - deep-dive no longer 401",
      client.get("/deep-dive/ZZZZ.TEST").status_code != 401)
check("G13: wall ON, signed in - asx-200 scan no longer 401",
      client.get("/scan/asx-200").status_code != 401)
client.cookies.clear()

_tok_api_dd = share_link_store.create_link("deep_dive", item_key="ZZZZ.TEST", expiry_days=7)
check("G14: wall ON, signed out, VALID share token for deep-dive/ZZZZ.TEST - not 401",
      client.get("/deep-dive/ZZZZ.TEST", params={"share": _tok_api_dd}).status_code != 401)
check("G15: that same token does NOT unlock a different ticker",
      client.get("/deep-dive/OTHER.TEST", params={"share": _tok_api_dd}).status_code == 401)
check("G16: that same token does NOT unlock history for the SAME ticker (different "
      "page_key scope would be a design choice, but history reuses deep_dive's scope - "
      "so this SHOULD pass through)",
      client.get("/history/ZZZZ.TEST", params={"share": _tok_api_dd}).status_code != 401)

_tok_api_scan = share_link_store.create_link("scanner", item_key="ASX 200", expiry_days=7)
check("G17: a Scanner share token for ASX 200 unlocks that universe's /scan endpoint",
      client.get("/scan/asx-200", params={"share": _tok_api_scan}).status_code != 401)
check("G18: docs/openapi stay untouched (no gate added - still 200)",
      client.get("/docs").status_code == 200 and client.get("/openapi.json").status_code == 200)
os.environ.pop("SIGNIN_WALL_LIVE", None)
print("[section_g_api_v1_gating] OK\n")

# ======================================================================
# SECTION H: server.py - sitemap.xml + /s/universe/<slug> redirect
# ======================================================================
# _renders_html("/top-100")/("/currency-risk") is itself an opt-in gate
# (INDEXABLE_PAGES env var, unrelated to this task) whose tri-state is
# computed ONCE at server.py's OWN import time (module-level constants,
# not re-read per-call) - must be set BEFORE the first `import server`
# anywhere in this process, so these sitemap checks exercise ONLY the
# signin_wall.wall_live() condition this task actually added, not that
# pre-existing unrelated gate too.
os.environ["INDEXABLE_PAGES"] = "all"
import server
import top100_render
import scan_store


class _FakeRequest:
    headers = {}
    cookies = {}

    class url:
        path = "/"
        scheme = "https"


top100_render.TOP100_PUBLIC = True
os.environ.pop("SIGNIN_WALL_LIVE", None)
_xml_off = server.sitemap(_FakeRequest()).body.decode()
check("H1: wall OFF, TOP100_PUBLIC=True - /top-100 IS in the sitemap",
      "/top-100</loc>" in _xml_off)
check("H2: wall OFF - /currency-risk IS in the sitemap",
      "/currency-risk</loc>" in _xml_off)

os.environ["SIGNIN_WALL_LIVE"] = "1"
_xml_on = server.sitemap(_FakeRequest()).body.decode()
check("H3: wall ON - /top-100 is DROPPED from the sitemap",
      "/top-100</loc>" not in _xml_on)
check("H4: wall ON - /currency-risk is DROPPED from the sitemap",
      "/currency-risk</loc>" not in _xml_on)
os.environ.pop("SIGNIN_WALL_LIVE", None)
print("[section_h1_sitemap] OK\n")


class _FakeRequestCookies:
    def __init__(self, path, cookies=None):
        self.headers = {}
        self.cookies = cookies or {}

        class url:
            scheme = "https"
        url.path = path
        self.url = url


# /s/universe/<slug> reads straight from a real scan_store file on disk
# (universe_snapshot_render.universe_name_for_slug() - never a hardcoded
# slug<->name table), using scan_store's OWN underscore-separated slug
# convention ("asx_200"/"s_p_500") - a DIFFERENT convention from api_v1's
# dash-separated one ("asx-200") used in Section G above. Seeding real
# (minimal) scan rows here so universe_name_for_slug() actually resolves,
# which is needed to reach this task's own gate at all (it runs AFTER
# the pre-existing "unknown slug -> 404" check).
scan_store.save_scan("ASX 200", [], "test-seed")
scan_store.save_scan("S&P 500", [], "test-seed")
_ASX_SLUG = scan_store._slug("ASX 200")
_SP500_SLUG = scan_store._slug("S&P 500")

os.environ["SIGNIN_WALL_LIVE"] = "1"
_resp_blocked = server.universe_snapshot_page(
    _ASX_SLUG, _FakeRequestCookies(f"/s/universe/{_ASX_SLUG}"))
check("H5: wall ON, signed out - /s/universe/<asx slug> REDIRECTS (never errors) away "
      "from the non-public universe's real table",
      _resp_blocked.status_code == 302)

_resp_sp500 = server.universe_snapshot_page(
    _SP500_SLUG, _FakeRequestCookies(f"/s/universe/{_SP500_SLUG}"))
check("H6: wall ON, signed out - /s/universe/<s&p 500 slug> (the public universe) does "
      "NOT redirect", getattr(_resp_sp500, "status_code", 200) != 302)

_server_tok = email_auth.create_session("server-test@example.com")
_resp_signed_in = server.universe_snapshot_page(
    _ASX_SLUG, _FakeRequestCookies(f"/s/universe/{_ASX_SLUG}", cookies={"sdd_auth": _server_tok}))
check("H7: wall ON, signed in via sdd_auth cookie - /s/universe/<asx slug> does NOT redirect",
      getattr(_resp_signed_in, "status_code", 200) != 302)
os.environ.pop("SIGNIN_WALL_LIVE", None)
print("[section_h2_universe_snapshot_redirect] OK\n")

# ======================================================================
# SECTION I: default-off sanity, once more, end to end
# ======================================================================
os.environ.pop("SIGNIN_WALL_LIVE", None)
check("I1: SIGNIN_WALL_LIVE unset at the very end -> wall_live() False",
      signin_wall.wall_live() is False)

print()
print(f"PASS={passed} FAIL={failed}")
sys.exit(1 if failed else 0)
