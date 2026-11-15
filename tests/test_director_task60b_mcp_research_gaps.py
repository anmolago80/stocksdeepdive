"""
TASK 60b - close two gaps in the sign-in wall (Director-directed/
owner-approved, 15 Nov 2026), done after the Director verified TASK 60
(340d8d3 / deploy 5b7d2ccd) live.

Covers:
  A. mcp_server.py - every tool (deep_dive, compare, moat, research_
     notes) blocked with a clear "Sign in required" ValueError (never a
     crash) while SIGNIN_WALL_LIVE is on; scan() stays public ONLY for
     S&P 500; wall off = every tool unchanged.
  B. api_v1.py - /blog and /blog/{slug} are PUBLIC again regardless of
     the wall (TASK 60's own blanket gate removed from just these two).
  C. server.py's /s/research/<slug> - gated like the Research page:
     signed-out -> redirect to /research?ticker=<ticker> (never an
     error), noindex while the wall is on, dropped from sitemap.xml
     while the wall is on, a valid per-ticker share token opens it, and
     the gate's page_key ("research_snapshot") is wired into app.py's
     new per-ticker "Create share link" control on page_research().
  D. /s/<ticker> stays untouched - source-inspection confirms no new
     signin_wall reference in snapshot_page(), and a functional check
     it still serves normally with the wall on, signed out.
  E. Wall off: A-D all behave exactly as before this task.

Run: python3 tests/test_director_task60b_mcp_research_gaps.py
"""
import inspect
import os
import sys
import tempfile
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) + "/..")
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

TESTVOL = tempfile.mkdtemp(prefix="task60b_test_")
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = TESTVOL
os.environ.pop("SIGNIN_WALL_LIVE", None)
os.environ["INDEXABLE_PAGES"] = "all"

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
# SECTION A: mcp_server.py tool gating
# ======================================================================
import mcp_server
import scan_store
import signin_wall

scan_store.save_scan("S&P 500", [{"Ticker": "AAPL", "Value Score": 80}], "test-seed")
scan_store.save_scan("ASX 200", [{"Ticker": "CSL.AX", "Value Score": 70}], "test-seed")

_SIGNIN_MSG = "Sign in required"

os.environ.pop("SIGNIN_WALL_LIVE", None)
try:
    mcp_server.deep_dive("ZZZZ.NOPE")
    check("A1: wall OFF - deep_dive() raises its OWN 'no snapshot' error, not the sign-in one",
          False)
except ValueError as e:
    check("A1: wall OFF - deep_dive() raises its OWN 'no snapshot' error, not the sign-in one",
          _SIGNIN_MSG not in str(e))

os.environ["SIGNIN_WALL_LIVE"] = "1"
try:
    mcp_server.deep_dive("ZZZZ.NOPE")
    check("A2: wall ON - deep_dive() is blocked with the sign-in message", False)
except ValueError as e:
    check("A2: wall ON - deep_dive() is blocked with the sign-in message", _SIGNIN_MSG in str(e))

try:
    mcp_server.compare(["AAPL"])
    check("A3: wall ON - compare() is blocked", False)
except ValueError as e:
    check("A3: wall ON - compare() is blocked", _SIGNIN_MSG in str(e))

try:
    mcp_server.moat("AAPL")
    check("A4: wall ON - moat() is blocked", False)
except ValueError as e:
    check("A4: wall ON - moat() is blocked", _SIGNIN_MSG in str(e))

try:
    mcp_server.research_notes("AAPL")
    check("A5: wall ON - research_notes() is blocked", False)
except ValueError as e:
    check("A5: wall ON - research_notes() is blocked", _SIGNIN_MSG in str(e))

try:
    mcp_server.scan("ASX 200")
    check("A6: wall ON - scan() for a NON-S&P-500 universe is blocked", False)
except ValueError as e:
    check("A6: wall ON - scan() for a NON-S&P-500 universe is blocked", _SIGNIN_MSG in str(e))

try:
    _sp500_result = mcp_server.scan("S&P 500")
    check("A7: wall ON - scan() for S&P 500 STAYS PUBLIC (no exception, real data returned)",
          _sp500_result["data"]["universe"] == "S&P 500")
except ValueError as e:
    check("A7: wall ON - scan() for S&P 500 STAYS PUBLIC (no exception, real data returned)",
          False)

os.environ.pop("SIGNIN_WALL_LIVE", None)
try:
    _off_result = mcp_server.scan("ASX 200")
    check("A8: wall OFF - scan() for ASX 200 works normally again",
          _off_result["data"]["universe"] == "ASX 200")
except ValueError:
    check("A8: wall OFF - scan() for ASX 200 works normally again", False)

try:
    mcp_server.compare(["AAPL"])
    check("A9: wall OFF - compare() no longer blocked", True)
except ValueError as e:
    check("A9: wall OFF - compare() no longer blocked", _SIGNIN_MSG not in str(e))
print("[section_a_mcp_gating] OK\n")

# ======================================================================
# SECTION B: api_v1.py /blog stays public
# ======================================================================
import api_v1
from fastapi.testclient import TestClient

client = TestClient(api_v1.api_app)

os.environ.pop("SIGNIN_WALL_LIVE", None)
check("B1: wall OFF - /blog is never 401", client.get("/blog").status_code != 401)

os.environ["SIGNIN_WALL_LIVE"] = "1"
check("B2: wall ON - /blog is STILL never 401 (public regardless of the wall)",
      client.get("/blog").status_code != 401)
check("B3: wall ON - /blog/{slug} is STILL never 401",
      client.get("/blog/some-slug").status_code != 401)
# Everything TASK 60 gated stays gated - this task only carved out blog.
check("B4: wall ON - /deep-dive/{ticker} is STILL 401 (unchanged)",
      client.get("/deep-dive/ZZZZ.TEST").status_code == 401)
os.environ.pop("SIGNIN_WALL_LIVE", None)
print("[section_b_api_blog_public] OK\n")

# ======================================================================
# SECTION C: server.py's /s/research/<slug> gate
# ======================================================================
import server
import share_link_store
import email_auth


class _FakeRequest:
    def __init__(self, path, query_string="", cookies=None):
        self.headers = {}
        self.cookies = cookies or {}

        class url:
            scheme = "https"
        url.path = path
        self.url = url

        class query_params(dict):
            pass
        qp = query_params()
        if query_string:
            for pair in query_string.split("&"):
                if "=" in pair:
                    k, v = pair.split("=", 1)
                    qp[k] = v
        self.query_params = qp


_FAKE_RESEARCH_DATA = {"fake": True}

with mock.patch.object(server.research_snapshot_render, "_load_research_data",
                        return_value=_FAKE_RESEARCH_DATA), \
     mock.patch.object(server.research_snapshot_render, "ticker_for_slug",
                        return_value="CSL.AX"), \
     mock.patch.object(server.research_snapshot_render, "render_research_snapshot",
                        return_value="<html>fake research page</html>"):

    os.environ.pop("SIGNIN_WALL_LIVE", None)
    _resp_off = server.research_snapshot_page("csl-commonwealth-serum-labs",
                                               _FakeRequest("/s/research/csl-commonwealth-serum-labs"))
    check("C1: wall OFF - /s/research/<slug> serves the real page (no redirect)",
          getattr(_resp_off, "status_code", 200) == 200)
    check("C2: wall OFF - no noindex header", "x-robots-tag" not in {
        k.lower() for k in _resp_off.headers.keys()})

    os.environ["SIGNIN_WALL_LIVE"] = "1"
    _resp_blocked = server.research_snapshot_page(
        "csl-commonwealth-serum-labs", _FakeRequest("/s/research/csl-commonwealth-serum-labs"))
    check("C3: wall ON, signed out, no token - REDIRECTS (never an error)",
          _resp_blocked.status_code == 302)
    check("C4: the redirect target is /research?ticker=<ticker> (never an error page)",
          _resp_blocked.headers["location"] == "/research?ticker=CSL.AX")

    _resp_es = server.research_snapshot_page(
        "csl-commonwealth-serum-labs", _FakeRequest("/es/s/research/csl-commonwealth-serum-labs"))
    check("C5: the ES twin redirects with &lang=es appended (never to a non-existent /es/research)",
          _resp_es.headers["location"] == "/research?ticker=CSL.AX&lang=es")

    _tok = share_link_store.create_link("research_snapshot", item_key="CSL.AX", expiry_days=7)
    _resp_shared = server.research_snapshot_page(
        "csl-commonwealth-serum-labs",
        _FakeRequest("/s/research/csl-commonwealth-serum-labs", query_string=f"share={_tok}"))
    check("C6: wall ON, signed out, VALID share token for this exact ticker - serves the page",
          getattr(_resp_shared, "status_code", 200) == 200)
    check("C7: that response carries noindex while the wall is on",
          _resp_shared.headers.get("x-robots-tag") == "noindex")

    _tok_other = share_link_store.create_link("research_snapshot", item_key="BHP.AX", expiry_days=7)
    _resp_wrong_tok = server.research_snapshot_page(
        "csl-commonwealth-serum-labs",
        _FakeRequest("/s/research/csl-commonwealth-serum-labs", query_string=f"share={_tok_other}"))
    check("C8: a share token scoped to a DIFFERENT ticker still redirects",
          _resp_wrong_tok.status_code == 302)

    _server_tok = email_auth.create_session("task60b-test@example.com")
    _resp_signed_in = server.research_snapshot_page(
        "csl-commonwealth-serum-labs",
        _FakeRequest("/s/research/csl-commonwealth-serum-labs",
                     cookies={"sdd_auth": _server_tok}))
    check("C9: wall ON, signed in via sdd_auth cookie - serves the page (no redirect)",
          getattr(_resp_signed_in, "status_code", 200) == 200)
    check("C10: a signed-in visitor's response STILL carries noindex (noindex is tied to the "
          "wall being live, not to who's viewing)",
          _resp_signed_in.headers.get("x-robots-tag") == "noindex")

    os.environ.pop("SIGNIN_WALL_LIVE", None)
print("[section_c_research_snapshot_gate] OK\n")

# sitemap.xml exclusion
with mock.patch.object(server.research_snapshot_render, "_load_research_data",
                        return_value=_FAKE_RESEARCH_DATA), \
     mock.patch.object(server.research_snapshot_render, "slug_map",
                        return_value={"csl-commonwealth-serum-labs": "CSL.AX"}):

    class _FakeSitemapRequest:
        headers = {}

        class url:
            path = "/"
            scheme = "https"

    os.environ.pop("SIGNIN_WALL_LIVE", None)
    _xml_off = server.sitemap(_FakeSitemapRequest()).body.decode()
    check("C11: wall OFF - /s/research/<slug> IS in the sitemap",
          "/s/research/csl-commonwealth-serum-labs</loc>" in _xml_off)

    os.environ["SIGNIN_WALL_LIVE"] = "1"
    _xml_on = server.sitemap(_FakeSitemapRequest()).body.decode()
    check("C12: wall ON - /s/research/<slug> is DROPPED from the sitemap",
          "/s/research/csl-commonwealth-serum-labs</loc>" not in _xml_on)
    os.environ.pop("SIGNIN_WALL_LIVE", None)
print("[section_c2_research_sitemap] OK\n")

# app.py's new per-ticker "Create share link" control on page_research()
from streamlit.testing.v1 import AppTest

import ai_gate
_OWNER = ai_gate.owner_email()


def _research_share_script(email):
    return f"""
import os, sys
sys.path.insert(0, {REPO_ROOT!r})
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = {TESTVOL!r}
os.environ["SCHEDULER_ENABLED"] = "false"
import paywall_engine
paywall_engine.current_user_email = lambda: {email!r}
import app
app._render_create_share_link_control(
    "research_snapshot", "CSL.AX", "/s/research/csl-commonwealth-serum-labs",
    "en", "research_snapshot",
)
"""


_before_links = len(share_link_store.list_links())
_at_owner_research = AppTest.from_string(_research_share_script(_OWNER), default_timeout=60)
_at_owner_research.run()
check("C13: the owner sees the 'Create share link' expander for a specific research write-up "
      "(importing app.py also runs its default Home page as a side effect, which can render "
      "its own unrelated expanders - checking by label, not by count)",
      any(e.label == "🔗 Create share link (owner only)"
          for e in _at_owner_research.get("expander")))
_at_owner_research.button(key="share_link_create_research_snapshot").click().run()
check("C14: clicking Create actually mints a research_snapshot-scoped link",
      len(share_link_store.list_links()) == _before_links + 1)
_latest = max(share_link_store.list_links(), key=lambda r: r["created_at"])
check("C15: the newly-minted link is scoped page_key='research_snapshot', item_key='CSL.AX'",
      _latest["page_key"] == "research_snapshot" and _latest["item_key"] == "CSL.AX")

_src = inspect.getsource(__import__("app").page_research)
check("C16: page_research() wires the new per-ticker control with the right page_key/item_key",
      '_render_create_share_link_control(\n        "research_snapshot", ticker,' in _src)
print("[section_c3_research_share_link_control] OK\n")

# ======================================================================
# SECTION D: /s/<ticker> stays untouched
# ======================================================================
_snapshot_src = inspect.getsource(server.snapshot_page)
check("D1: snapshot_page() (/s/<ticker>) was NOT given any signin_wall reference",
      "signin_wall" not in _snapshot_src)

import snapshot_store

snapshot_store.get_snapshot = getattr(snapshot_store, "get_snapshot", None)
with mock.patch.object(server.snapshot_store, "get_snapshot",
                        return_value={"ticker": "CSL.AX", "generated_at": "2026-11-15T00:00:00",
                                      "data": {}, "moat": None}):
    class _FakeTickerRequest:
        headers = {}
        cookies = {}

        class url:
            path = "/s/CSL.AX"
            scheme = "https"

    os.environ["SIGNIN_WALL_LIVE"] = "1"
    _resp_ticker = server.snapshot_page("CSL.AX", _FakeTickerRequest())
    check("D2: wall ON, signed out - /s/<ticker> still serves normally (never gated)",
          getattr(_resp_ticker, "status_code", 200) == 200)
    os.environ.pop("SIGNIN_WALL_LIVE", None)
print("[section_d_ticker_snapshot_unchanged] OK\n")

# No research-note text embedded in the /s/<ticker> renderer at all -
# confirmed by source inspection of the renderer module itself (not
# just the route), since that's where such a leak would actually live.
import snapshot_render

_render_src = inspect.getsource(snapshot_render)
check("D3: snapshot_render.py has no blog_store/research-note-content import or reference "
      "(only blog_render's own shared page-shell helpers, which carry no note TEXT) - "
      "confirms no research-note text is embedded in /s/<ticker>",
      "blog_store" not in _render_src and "posts_for_ticker" not in _render_src)

print()
print(f"PASS={passed} FAIL={failed}")
sys.exit(1 if failed else 0)
