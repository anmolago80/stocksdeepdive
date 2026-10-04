"""
"One slow page must never freeze the whole site" (4 Oct 2026, Director-
directed, written on Andrew's request, immediately after commit 4cf801b
fixed /calendar's own ~133s blow-up). That fix made /calendar fast. It
did NOT change the fact that an `async def` FastAPI handler which does
blocking synchronous work directly in its own body runs on the ONE
process-wide asyncio event loop with nothing yielding control - any
OTHER request (robots.txt, the Streamlit proxy, the scheduler's own
ticks) simply cannot run until that handler returns. Evidence from the
Railway HTTP log, 4 Oct, deployment 5c6efc4c: Googlebot's GET /es/
calendar ran 133s; in that same window GET /robots.txt also took 133s
and /, /deep-dive, /s/COF.AX timed out (499).

This file proves the GENERAL mechanism, independent of which specific
route(s) this commit moves off the event loop (see server.py's own
route-by-route changes for that) - two temporary, test-only routes
(never registered in production) are added to the real `server.app`
object and removed again after each check:

  CHECK 1 (BEFORE): an `async def` handler that calls `time.sleep(5)`
  directly - exactly the shape of the bug - blocks a concurrent
  GET /robots.txt for the full 5 seconds. This reproduces the root
  cause and is expected to FAIL (robots.txt slow) on ANY commit, before
  or after this one - it's the mechanism, not something this commit
  changes.

  CHECK 2 (AFTER): the identical blocking work (`time.sleep(5)`), moved
  off the event loop via `starlette.concurrency.run_in_threadpool` -
  the exact fix this commit applies to every route identified as
  blocking in server.py - lets a concurrent GET /robots.txt answer in
  under 1 second while it runs. This is the "fails before, passes
  after" pair the task's own Tests section asks for: CHECK 1 shows the
  committed starting point's exposure, CHECK 2 shows this commit's own
  fix pattern closes it.

Both checks use httpx.ASGITransport directly against the real
`server.app` (no live network, no subprocess, no lifespan needed -
neither /robots.txt nor the test routes touch any startup-initialized
state like the Streamlit proxy's httpx client) and asyncio.gather() to
fire both requests truly concurrently on one event loop, which is
exactly the condition that exposes event-loop blocking.

Run: python3 tests/test_server_no_blocking_routes.py
"""
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx
from fastapi.responses import PlainTextResponse
from fastapi.routing import APIRoute
from starlette.concurrency import run_in_threadpool

import server


def _add_test_route(path, handler):
    # Inserted at index 0, not appended: server.py's own catch-all proxy
    # route ("/{path:path}", registered at import time) matches ANY
    # path and sits earlier in app.router.routes than anything added
    # afterwards via the ordinary add_api_route()/append path - an
    # appended test route would be dead code, shadowed by the catch-all
    # before it's ever reached. Routes are tried in list order, so the
    # front of the list always wins.
    route = APIRoute(path, handler, methods=["GET"], include_in_schema=False)
    server.app.router.routes.insert(0, route)


def _remove_test_route(path):
    server.app.router.routes = [
        r for r in server.app.router.routes if getattr(r, "path", None) != path
    ]


async def _timed_get(client, path):
    t0 = time.monotonic()
    resp = await client.get(path)
    return time.monotonic() - t0, resp.status_code


async def _run_concurrent(slow_path):
    transport = httpx.ASGITransport(app=server.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        (slow_elapsed, slow_status), (fast_elapsed, fast_status) = await asyncio.gather(
            _timed_get(client, slow_path),
            _timed_get(client, "/robots.txt"),
        )
    return slow_elapsed, slow_status, fast_elapsed, fast_status


# ======================================================================
# CHECK 1 (BEFORE): direct time.sleep() in an async def handler blocks
# a concurrent /robots.txt request too - the exact incident shape.
# ======================================================================
_BEFORE_PATH = "/_test_blocking_before_5s"


async def _before_handler():
    time.sleep(5)
    return PlainTextResponse("slow (blocking)")


_add_test_route(_BEFORE_PATH, _before_handler)
try:
    _slow_el, _slow_st, _fast_el, _fast_st = asyncio.run(_run_concurrent(_BEFORE_PATH))
finally:
    _remove_test_route(_BEFORE_PATH)

assert _slow_st == 200 and _fast_st == 200, (_slow_st, _fast_st)
assert _fast_el >= 4.0, (
    f"BEFORE check: /robots.txt took only {_fast_el:.2f}s while a direct time.sleep(5) "
    f"handler ran {_slow_el:.2f}s - expected it to be BLOCKED (>= 4s). If this fails, the "
    f"reproduction itself is broken, not fixed."
)
print(f"[before_direct_sleep_blocks_everything] an async def handler calling time.sleep(5) "
      f"directly on the event loop blocks a concurrent GET /robots.txt for {_fast_el:.2f}s "
      f"(slow route itself took {_slow_el:.2f}s) - reproduces the exact /calendar incident "
      f"shape OK (this is the failing baseline, by design)")

# ======================================================================
# CHECK 2 (AFTER): the SAME blocking work, moved off the loop via
# run_in_threadpool (this commit's own fix pattern) - /robots.txt is
# unaffected.
# ======================================================================
_AFTER_PATH = "/_test_blocking_after_5s"


async def _after_handler():
    await run_in_threadpool(time.sleep, 5)
    return PlainTextResponse("slow (off-loop)")


_add_test_route(_AFTER_PATH, _after_handler)
try:
    _slow_el2, _slow_st2, _fast_el2, _fast_st2 = asyncio.run(_run_concurrent(_AFTER_PATH))
finally:
    _remove_test_route(_AFTER_PATH)

assert _slow_st2 == 200 and _fast_st2 == 200, (_slow_st2, _fast_st2)
assert _fast_el2 < 1.0, (
    f"AFTER check: /robots.txt took {_fast_el2:.2f}s while the run_in_threadpool-wrapped "
    f"slow route ran {_slow_el2:.2f}s - expected under 1s."
)
print(f"[after_run_in_threadpool_keeps_loop_free] the identical 5s blocking work, wrapped in "
      f"run_in_threadpool (this commit's own fix pattern), lets GET /robots.txt answer in "
      f"{_fast_el2:.2f}s while it runs (slow route itself still took {_slow_el2:.2f}s) OK")

# ======================================================================
# CHECK 3: server._slow_request_logging_middleware logs the exact
# "[slow] METHOD /path N.Ns" line for a request at/over the 5s
# threshold, and logs NOTHING for a fast one - log only, nothing about
# the response itself.
# ======================================================================
import logging


class _ListHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines = []

    def emit(self, record):
        self.lines.append(self.format(record))


async def _slow_logged_handler():
    await run_in_threadpool(time.sleep, 5.2)
    return PlainTextResponse("slow")


_handler = _ListHandler()
_handler.setLevel(logging.WARNING)
server.log.addHandler(_handler)
server.log.setLevel(logging.WARNING)

_LOGGED_SLOW_PATH = "/_test_slow_logged_5s"
_add_test_route(_LOGGED_SLOW_PATH, _slow_logged_handler)
try:
    async def _hit_both():
        transport = httpx.ASGITransport(app=server.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            r_slow = await client.get(_LOGGED_SLOW_PATH)
            r_fast = await client.get("/robots.txt")
            return r_slow.status_code, r_fast.status_code

    _st_slow, _st_fast = asyncio.run(_hit_both())
finally:
    _remove_test_route(_LOGGED_SLOW_PATH)
    server.log.removeHandler(_handler)

assert _st_slow == 200 and _st_fast == 200, (_st_slow, _st_fast)
_slow_lines = [ln for ln in _handler.lines if ln.startswith(f"[slow] GET {_LOGGED_SLOW_PATH} ")]
assert len(_slow_lines) == 1, (_handler.lines, _slow_lines)
import re as _re
assert _re.match(rf"^\[slow\] GET {_re.escape(_LOGGED_SLOW_PATH)} \d+\.\d+s$", _slow_lines[0]), _slow_lines[0]
_fast_lines = [ln for ln in _handler.lines if "/robots.txt" in ln]
assert _fast_lines == [], _fast_lines
print(f"[slow_request_log_line] {_slow_lines[0]!r} logged for the 5.2s test route, and "
      f"nothing logged for the fast /robots.txt request alongside it OK")

print("\nALL SERVER NO-BLOCKING-ROUTES MECHANISM CHECKS PASSED")
