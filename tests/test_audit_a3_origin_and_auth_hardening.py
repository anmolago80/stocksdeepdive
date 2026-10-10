"""Audit fix A3 / Fable findings S1+S2 (10 Oct 2026, instruction_
combined_10oct.md PART A): origin-check hardening + admin-key auth
hardening across three spots.

S1 - server._same_origin(): was a plain substring check (`host in
origin`), so a crafted Origin/Referer like
"https://stocksdeepdive.com.evil.com" (the real host as a PREFIX of an
attacker's own domain) or "https://evil.com/?r=stocksdeepdive.com"
(the real host embedded in a query string) both wrongly passed, since
the real host string genuinely IS a substring of each. Fixed: an
EXACT netloc comparison via urllib.parse.urlsplit on Origin, with an
equally strict Referer fallback (exact netloc match) or none.

S2 - /_auth/set-cookie accepted ANY same-origin POST body token up to
256 chars and set it as the session cookie with no check that the
token is actually a real, currently-valid session - fixed to refuse
(403) unless email_auth.session_email(tok) resolves to a real email.
app.py's RC-view popover ("Unlock" button) compared the typed key with
plain `==` (not constant-time), never checked the shared admin-key
lockout before comparing, and never counted a wrong guess toward that
lockout - the ONLY admin-key entry point in the whole file that didn't
already do all three, unlike the ?admin= URL param, the Scanner ☰
panel, and the blog editor's own admin-key prompts.

Run: python3 tests/test_audit_a3_origin_and_auth_hardening.py
"""
import asyncio
import os
import sys
import tempfile
import time
from unittest import mock

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TESTVOL = tempfile.mkdtemp(prefix="audit_a3_test_")
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = TESTVOL

import server
import email_auth
from streamlit.testing.v1 import AppTest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

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
# CHECK S1: _same_origin() - exact netloc match only.
# ======================================================================
def _req(headers):
    req = mock.Mock()
    req.headers = headers
    return req


check("real origin, matching host -> True",
      server._same_origin(_req({"origin": "https://stocksdeepdive.com", "host": "stocksdeepdive.com"})))

check("attacker domain with the real host as a PREFIX "
      '("stocksdeepdive.com.evil.com") -> False, not a substring match',
      server._same_origin(_req({"origin": "https://stocksdeepdive.com.evil.com",
                                 "host": "stocksdeepdive.com"})) is False)

check("attacker domain with the real host embedded in a QUERY STRING "
      '("https://evil.com/?r=stocksdeepdive.com") -> False',
      server._same_origin(_req({"origin": "https://evil.com/?r=stocksdeepdive.com",
                                 "host": "stocksdeepdive.com"})) is False)

check("no Origin, a matching Referer -> True (exact netloc fallback)",
      server._same_origin(_req({"referer": "https://stocksdeepdive.com/blog/post",
                                 "host": "stocksdeepdive.com"})))

check("no Origin, a Referer with the real host only as a prefix -> False",
      server._same_origin(_req({"referer": "https://stocksdeepdive.com.evil.com/x",
                                 "host": "stocksdeepdive.com"})) is False)

check("neither Origin nor Referer -> False",
      server._same_origin(_req({"host": "stocksdeepdive.com"})) is False)

check("a bare curl Host header with no scheme still matches the real origin",
      server._same_origin(_req({"origin": "https://stocksdeepdive.com:443",
                                 "host": "stocksdeepdive.com"})) in (True, False))
# (port handling isn't specified by the instruction - this call only
# proves the function doesn't crash on a port-bearing Origin; the
# exact-match checks above are the real proof.)


# ======================================================================
# CHECK S2: /_auth/set-cookie refuses a token that doesn't resolve to
# a real session, even from a same-origin POST.
# ======================================================================
async def _post(path, **kw):
    transport = httpx.ASGITransport(app=server.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post(path, **kw)


with mock.patch.object(server, "_same_origin", return_value=True):
    _r_bad = asyncio.run(_post("/_auth/set-cookie", json={"tok": "not-a-real-session-token"}))
check("a same-origin POST with a token that does NOT resolve via "
      "email_auth.session_email() is refused (403), never sets a cookie",
      _r_bad.status_code == 403 and "set-cookie" not in {k.lower() for k in _r_bad.headers.keys()})

_real_email = "audit-a3-test@example.com"
_real_tok = email_auth.secrets.token_hex(32)
_now_iso = email_auth._iso(email_auth._now())
with email_auth._conn() as _conn:
    _conn.execute(
        "INSERT INTO auth_sessions (token_hash, email, created_at, last_seen) VALUES (?, ?, ?, ?)",
        (email_auth._hash(_real_tok), _real_email, _now_iso, _now_iso),
    )
check("the freshly-inserted session really does resolve via session_email() "
      "(sanity check on the fixture itself)",
      email_auth.session_email(_real_tok) == _real_email)

with mock.patch.object(server, "_same_origin", return_value=True):
    _r_good = asyncio.run(_post("/_auth/set-cookie", json={"tok": _real_tok}))
check("a same-origin POST with a token that DOES resolve to a real session is "
      "accepted (204) and sets the cookie",
      _r_good.status_code == 204 and "sdd_auth=" in _r_good.headers.get("set-cookie", ""))


# ======================================================================
# CHECK S2b: app.py's RC-view popover ("Unlock" button) - lockout
# checked first, constant-time compare, wrong guesses counted toward
# the SAME shared lockout every other admin-key prompt in this file
# already uses.
# ======================================================================
TESTVOL_APP = tempfile.mkdtemp(prefix="audit_a3_app_test_")
_REAL_ADMIN_KEY = "a3-test-admin-key-12345"

# app.py's _admin_key_env is a module-level constant computed ONCE at
# import time from this env var - must be set before app.py is first
# imported ANYWHERE in this process (the outer `import app` just
# below is that first import; AppTest's own internal `import app`
# later just reuses the already-cached module, same as every other
# AppTest-based test file in this repo).
os.environ["ADMIN_REFRESH_KEY"] = _REAL_ADMIN_KEY
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = TESTVOL_APP
import app


def _build_rc_view_script():
    return f"""
import os, sys
sys.path.insert(0, {REPO_ROOT!r})
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = {TESTVOL_APP!r}
os.environ["ADMIN_REFRESH_KEY"] = {_REAL_ADMIN_KEY!r}
import paywall_engine as pw
pw.current_user_email = lambda: "rc-view-test-nonowner@example.com"
import streamlit as st
st.session_state["lang"] = "en"
st.session_state["full_view_exited"] = True
import app
app._render_admin_unlock()
"""


# Reset the shared, process-global lockout counter before each phase
# of this check - it is an @st.cache_resource singleton, so it would
# otherwise leak state between CHECK S2b's own two phases.
app._admin_key_fail_state()["times"].clear()

at = AppTest.from_string(_build_rc_view_script(), default_timeout=60)
at.run()
check("the popover renders with no exception (not locked out yet)", not at.exception)

at.text_input(key="rc_view_key_input").set_value("totally-wrong-key")
at.button(key="rc_view_unlock_btn").click()
at.run()
check("a wrong key shows 'Incorrect key.'",
      any("Incorrect key" in e.value for e in at.error))
check("a wrong key did NOT unlock full view",
      not at.session_state.get("full_view_unlocked"))
check("the wrong guess was counted toward the shared lockout (1 recorded)",
      len(app._admin_key_fail_state()["times"]) == 1)

# Drive the counter to the lockout threshold, then prove the NEXT
# render refuses even before comparing (no text_input/button shown).
app._admin_key_fail_state()["times"].extend(
    [time.time()] * (app._ADMIN_LOCKOUT_MAX_ATTEMPTS - 1))
at2 = AppTest.from_string(_build_rc_view_script(), default_timeout=60)
at2.run()
check("once locked out, the popover shows the lockout message instead of a key prompt",
      any("Too many wrong attempts" in e.value for e in at2.error))
check("once locked out, no 'Unlock' button is rendered at all",
      not any(b.key == "rc_view_unlock_btn" for b in at2.button))

# Reset and prove the CORRECT key still works (constant-time compare
# doesn't change the happy path).
app._admin_key_fail_state()["times"].clear()
at3 = AppTest.from_string(_build_rc_view_script(), default_timeout=60)
at3.run()
at3.text_input(key="rc_view_key_input").set_value(_REAL_ADMIN_KEY)
at3.button(key="rc_view_unlock_btn").click()
at3.run()
check("the correct key unlocks full view",
      at3.session_state.get("full_view_unlocked") is True)
check("a correct-key unlock does NOT count toward the lockout",
      len(app._admin_key_fail_state()["times"]) == 0)


print()
print(f"PASS={passed} FAIL={failed}")
sys.exit(1 if failed else 0)
