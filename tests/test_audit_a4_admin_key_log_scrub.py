"""Audit fix A4 / Fable finding S3 (10 Oct 2026, instruction_combined_
10oct.md PART A): the admin key must never reach the logs.

Two distinct log sources carry the FULL request URL, query string
included, at a level that reaches server.py's own
logging.basicConfig(level=logging.INFO) root config:
  1. uvicorn's own access log (every request this ASGI app receives,
     e.g. a direct "GET /?admin=SECRET" before _proxy() ever runs).
  2. httpx's own internal "HTTP Request: GET http://127.0.0.1:8501/
     ...?..." INFO-level log line, emitted by server._client.send()
     every time _proxy() forwards a request (including its own query
     string) to the Streamlit backend - confirmed present in this
     site's real production logs with the admin key visible in full.

Per the instruction's own stated fallback ("if redacting is
impractical, use access_log=False plus the httpx logger at WARNING"):
redacting httpx's own internal log FORMAT string isn't something
server.py can reach into (it's generated inside the httpx library
itself) - so the fix raises the httpx logger's own level to WARNING
(its "HTTP Request: ..." line is INFO, so it's now filtered before
ever reaching the root handler) and passes access_log=False to
uvicorn.run() (uvicorn's own request-line log is now disabled
entirely). Paths server.py logs ITSELF (e.g. log.warning("upstream
error for %s: ...", request.url.path) - note: .path only, never
.query or the full URL) were already query-string-free before this
fix and are unaffected.

Run: python3 tests/test_audit_a4_admin_key_log_scrub.py
"""
import logging
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TESTVOL = tempfile.mkdtemp(prefix="audit_a4_test_")
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = TESTVOL

import httpx

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


SECRET = "SUPERSECRETADMINKEY123"


class _CaptureHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(self.format(record))


# ======================================================================
# CHECK 1: importing server.py raises the httpx logger's own level to
# WARNING - its INFO-level "HTTP Request: ..." line is now filtered
# before it ever reaches a handler.
# ======================================================================
check("logging.getLogger('httpx').level is WARNING or higher after importing server",
      logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING)

# ======================================================================
# CHECK 2: a real httpx request carrying the admin key in its query
# string - via a MockTransport, so no real network call is made, but
# httpx's own internal logging still fires exactly as it does for a
# real request - produces NO captured log line containing the secret,
# anywhere in the logging hierarchy (root included, since httpx's
# logger propagates by default).
# ======================================================================
def _handler(request):
    return httpx.Response(200, text="ok")


import asyncio


async def _make_request():
    transport = httpx.MockTransport(_handler)
    async with httpx.AsyncClient(transport=transport) as client:
        await client.get(f"http://127.0.0.1:8501/?admin={SECRET}")


def _capture_one_request():
    cap = _CaptureHandler()
    cap.setLevel(logging.DEBUG)
    logging.getLogger().addHandler(cap)
    try:
        asyncio.run(_make_request())
    finally:
        logging.getLogger().removeHandler(cap)
    return cap.records


# 2a - sanity check on the harness + the original bug mechanism: with
# the httpx logger temporarily forced back to its old (unfixed) INFO
# level, the exact same request DOES log the secret - proving this
# harness genuinely exercises httpx's own internal logging call, not
# a vacuous no-op.
_httpx_logger = logging.getLogger("httpx")
_real_level = _httpx_logger.level
_httpx_logger.setLevel(logging.INFO)
try:
    _records_unfixed = _capture_one_request()
finally:
    _httpx_logger.setLevel(_real_level)
check("(sanity) with the httpx logger forced back to INFO (the old, unfixed "
      "level), the admin key DOES appear in a captured log line - proves "
      "this harness and the underlying bug mechanism are both real",
      any(SECRET in line for line in _records_unfixed))

# 2b - the real check: at whatever level server.py's own fix actually
# left the httpx logger at, the same request logs nothing containing
# the secret.
_records_fixed = _capture_one_request()
check(f"no captured log line anywhere contains the admin key ({SECRET})",
      not any(SECRET in line for line in _records_fixed))


# ======================================================================
# CHECK 3: uvicorn.run() is called with access_log=False - source-level
# check, since actually invoking the __main__ block would bind a real
# port.
# ======================================================================
with open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "server.py")) as f:
    _src = f.read()


def _extract_balanced_call(src, needle):
    """The full `needle(...)` call text, matching parens by depth (not
    regex) so a parenthesized comment inside the call - e.g. "(10 Oct
    2026)" - doesn't prematurely end the match at its own first ')'."""
    start = src.find(needle)
    if start == -1:
        return None
    open_paren = src.index("(", start)
    depth = 0
    for i in range(open_paren, len(src)):
        if src[i] == "(":
            depth += 1
        elif src[i] == ")":
            depth -= 1
            if depth == 0:
                return src[start:i + 1]
    return None


_run_call_text = _extract_balanced_call(_src, "uvicorn.run(")
check("found the uvicorn.run(app, ...) call in server.py", _run_call_text is not None)
if _run_call_text:
    check("uvicorn.run(...) passes access_log=False",
          "access_log=False" in _run_call_text or "access_log = False" in _run_call_text)


# ======================================================================
# CHECK 4: server.py's OWN log lines about a proxied request only ever
# log request.url.path, never the query string or full URL - already
# true before this fix, confirmed still true (no regression).
# ======================================================================
check("server.py's own upstream-error log call uses request.url.path, "
      "never request.url or request.url.query",
      'request.url.path' in _src and 'log.warning("upstream error for %s: %s", request.url.path' in _src)

print()
print(f"PASS={passed} FAIL={failed}")
sys.exit(1 if failed else 0)
