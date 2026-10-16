"""
Director task (16 Oct 2026): stop "//" paths crashing the proxy.

Root cause, verified in production logs (13 Oct and 15 Oct 15:58 UTC):
a bot request whose path starts with "//" (e.g. "//wp-login.php")
reached server._proxy(), where `httpx.URL(path=request.url.path, ...)`
raises "InvalidURL: Relative URLs cannot have a path starting with
'//'" - unhandled, a 500 for every such request.

Fix: at the very top of _proxy(), before touching httpx/_client at
all, a path starting with "//" returns a plain 404. Nothing else in
the function changes.

Run: python3 tests/test_director_task2_double_slash_proxy_404.py
"""
import asyncio
import os
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx
from starlette.requests import Request

import server

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


def _make_request(path, query_string=b""):
    scope = {
        "type": "http",
        "method": "GET",
        "path": path,
        "raw_path": path.encode(),
        "query_string": query_string,
        "headers": [],
        "client": ("127.0.0.1", 12345),
        "server": ("testserver", 80),
        "scheme": "http",
    }

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    return Request(scope, receive)


# ======================================================================
# CHECK 1: a "//"-prefixed path returns a plain 404, with no exception
# - and WITHOUT ever touching server._client (the guard fires before
# the pre-existing "assert _client is not None" line, let alone the
# httpx.URL(...) construction that used to raise InvalidURL here).
# ======================================================================
_old_client = server._client
# A non-None sentinel (not a real httpx.AsyncClient) - if the new guard
# didn't fire FIRST, the very next line of _proxy() ("assert _client is
# not None") would pass right through it, and the line after that
# (httpx.URL(path=request.url.path, ...)) would then raise the EXACT
# production crash this task fixes (httpx.InvalidURL: "Relative URLs
# cannot have a path starting with '//'") - reproduced below by
# reverting server.py's fix and re-running this exact test.
server._client = object()
try:
    req = _make_request("//wp-login.php")
    resp = asyncio.run(server._proxy(req))
    check("a '//wp-login.php' request returns a 404, no exception raised",
          resp.status_code == 404)
finally:
    server._client = _old_client

# A handful of other "//"-prefixed shapes a bot might send, same guard.
for _bad_path in ("//", "//etc/passwd", "///triple-slash", "//wp-admin/setup-config.php"):
    server._client = object()
    try:
        resp = asyncio.run(server._proxy(_make_request(_bad_path)))
        check(f"{_bad_path!r} also returns 404, no exception",
              resp.status_code == 404)
    finally:
        server._client = _old_client


# ======================================================================
# CHECK 2: an ordinary (single-leading-slash) path is UNCHANGED - it
# still reaches the real proxy logic (httpx.URL construction, _client.
# build_request/.send), not the new 404 shortcut. Proven by mocking
# _client.send() to raise httpx.RequestError (the SAME pre-existing
# handler this function already had, at the "except httpx.RequestError"
# branch a few lines down) and confirming we land THERE (502), not in
# the new "//" branch (404) and not with an unhandled InvalidURL.
# ======================================================================
class _FakeClient:
    def build_request(self, method, url, headers=None, content=None):
        return object()  # never inspected by the mocked send() below

    async def send(self, req, stream=True):
        raise httpx.RequestError("simulated upstream failure")


server._client = _FakeClient()
try:
    resp_normal = asyncio.run(server._proxy(_make_request("/favicon.ico")))
    check("an ordinary path ('/favicon.ico') still reaches the real proxy "
          "logic and its pre-existing httpx.RequestError handler (502) - "
          "proving it did NOT take the new '//' 404 shortcut",
          resp_normal.status_code == 502)
finally:
    server._client = _old_client

# Same proof for a path that merely CONTAINS "//" later on (e.g. a
# double slash in a query-adjacent position) but does not START with
# it - must be completely unaffected by this fix.
server._client = _FakeClient()
try:
    resp_mid = asyncio.run(server._proxy(_make_request("/assets//logo.png")))
    check("a path with '//' in the MIDDLE (not at the start) is unaffected "
          "- still reaches the real proxy logic, not the new guard",
          resp_mid.status_code == 502)
finally:
    server._client = _old_client


print()
print(f"PASS={passed} FAIL={failed}")
sys.exit(1 if failed else 0)
