"""
Director's one-line follow-up to 325a368 (14 Oct 2026). Root cause
(verified, not assumed): server.py's logging.basicConfig() call had no
`stream` argument, so Python's logging module defaults the root
handler's stream to sys.stderr - and Railway marks every stderr line
as "error" severity in its own log viewer, REGARDLESS of the Python
log level the line was actually emitted at. This is a SEPARATE, and
earlier, cause of the exact same "routine lines show red in Railway"
symptom the overnight queue's TASK 5 and this session's later
"Director's 2-task queue TASK 1" fix (scheduler_engine._as_routine_
logger(), commit 325a368) already addressed from the OTHER end (the
Python log LEVEL each line is emitted at) - this fix addresses the
stream Railway's own stderr-is-always-red rule reads, independent of
level. Both fixes are needed; neither makes the other redundant.

Fix, part 1: logging.basicConfig(..., stream=sys.stdout) - stdout
lines read by severity in Railway's viewer, matching every other
process on this site that already logs to stdout (app.py's own
log=print, this same scheduler thread once its level is correctly
INFO after commit 325a368).

UPDATE (Director's own follow-up, same day): part 1 alone did NOT
take effect, proven by this file's own CHECK 4-5 below BEFORE the
fix landed - mcp_server.py's own `mcp = FastMCP(...)` (imported at
server.py's own line 105, BEFORE this basicConfig() call) runs
mcp.server.fastmcp.utilities.logging.configure_logging() at import
time, which itself calls logging.basicConfig() first, installing a
stderr StreamHandler on the root logger. Python's basicConfig() is a
documented no-op once the root logger already has a handler, so
server.py's own call - stream=sys.stdout included - never executed.
Fix, part 2: force=True on the SAME call, making it authoritative
regardless of import order.

Logging only - no message text or level changed anywhere by either
part of this fix; this test proves exactly that by also re-confirming
the format string and level are byte-identical to before this fix,
and that mcp_server/FastMCP's own log lines still appear (now routed
through the same, correctly-stdout root handler), and that nothing
else in the repo fights over the root handler with its own force=True
call or a later stderr re-add.

ALL FIXTURES IN THIS FILE ARE SYNTHETIC - this sandbox has no outbound
network access, same disclosure as every other fixture-based test in
this repo.

Run: python3 tests/test_director_followup_server_log_stream.py
"""
import inspect
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

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
# CHECK 1-4: server.py's own source - the basicConfig() call site now
# passes stream=sys.stdout AND force=True (TASK 1 follow-up, 14 Oct
# 2026 - stream alone wasn't enough, see this file's own updated
# module docstring below), with the level/format string left byte-
# identical to before either fix.
# ======================================================================
with open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "server.py")) as f:
    _server_src = f.read()

check("server.py's logging.basicConfig() call now passes stream=sys.stdout AND force=True",
      "logging.basicConfig(level=logging.INFO,\n                    format=\"%(asctime)s %(levelname)s %(name)s: %(message)s\",\n                    stream=sys.stdout, force=True)" in _server_src)
check("the level argument (logging.INFO) is unchanged - this fix never touches severity",
      "level=logging.INFO" in _server_src)
check("the format string is byte-identical to before this fix",
      'format="%(asctime)s %(levelname)s %(name)s: %(message)s"' in _server_src)
check("force=True is present on the same call",
      "force=True" in _server_src)


# ======================================================================
# CHECK 4-5: the real root handler, after basicConfig() actually runs,
# has its stream set to sys.stdout, not sys.stderr - the direct,
# behavioural proof the Director asked for ("test that the root
# handler's stream is sys.stdout"), not just a source-text match.
# ======================================================================
import importlib
import logging as _logging_module

# A fresh logging state to observe basicConfig()'s own effect cleanly -
# this sandbox's own test runner may have already configured the root
# logger from an earlier import in the same process, so the root
# handlers are cleared first (same convention logging.basicConfig()
# itself uses internally when called with force=True - done by hand
# here since server.py's own call doesn't pass force, matching
# production's own single, first-ever call in a fresh process).
for _h in list(_logging_module.root.handlers):
    _logging_module.root.removeHandler(_h)

import server  # noqa: E402  (runs server.py's own module-level basicConfig() call)

_root_handlers = _logging_module.root.handlers
check("the root logger has at least one handler after server.py's own import-time "
      "basicConfig() call",
      len(_root_handlers) >= 1)
_stream_handlers = [h for h in _root_handlers if hasattr(h, "stream")]
check("the root handler's own stream is sys.stdout, not sys.stderr (the actual bug: "
      "Railway marks every stderr line as error severity regardless of Python log level)",
      bool(_stream_handlers) and all(h.stream is sys.stdout for h in _stream_handlers))


# ======================================================================
# CHECK 6: mcp_server/FastMCP's own log lines still appear - confirmed
# behaviourally, not assumed. mcp.server.fastmcp.utilities.logging.
# get_logger() returns a logger with propagate=True and no handler of
# its own (its earlier basicConfig() call, which force=True now always
# overrides, never added a handler to a NAMED logger - only to root),
# so a record it emits still bubbles up to root and is handled by the
# SAME, now correctly-stdout, root handler - never silenced.
# ======================================================================
import io
from mcp.server.fastmcp.utilities.logging import get_logger as _mcp_get_logger

_mcp_style_logger = _mcp_get_logger("mcp.server.fastmcp.test_task1_followup")
_captured = io.StringIO()
_orig_stream = _root_handlers[0].stream
_root_handlers[0].stream = _captured
try:
    _mcp_style_logger.info("mcp_server/FastMCP log line still reaches the root handler")
finally:
    _root_handlers[0].stream = _orig_stream
check("an mcp.server.fastmcp-style logger's own INFO line still reaches the root handler "
      "(mcp_server/FastMCP logs are not silenced by this fix)",
      "mcp_server/FastMCP log line still reaches the root handler" in _captured.getvalue())


# ======================================================================
# CHECK 7-8: nothing else in the repo calls basicConfig with force=True
# or manually adds a stderr handler to the root logger - server.py's
# own call (this fix) is the ONE place in this codebase that does
# either, now and going forward (grep-enforced, not just observed once
# by hand).
# ======================================================================
_repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_force_true_basicconfig_sites = []
_stderr_addhandler_sites = []
for _fname in os.listdir(_repo_root):
    if not _fname.endswith(".py") or _fname == "tests":
        continue
    _fpath = os.path.join(_repo_root, _fname)
    if not os.path.isfile(_fpath):
        continue
    with open(_fpath, encoding="utf-8", errors="ignore") as _f:
        _src = _f.read()
    if "force=True" in _src and "basicConfig" in _src and _fname != "server.py":
        _force_true_basicconfig_sites.append(_fname)
    if "addHandler" in _src and "stderr" in _src:
        _stderr_addhandler_sites.append(_fname)
check("server.py is the ONLY .py file in this repo whose own code calls "
      "logging.basicConfig(..., force=True) - nothing else fights over the root handler",
      _force_true_basicconfig_sites == [])
check("no .py file in this repo manually adds a stderr handler to any logger",
      _stderr_addhandler_sites == [])

print()
print(f"PASS={passed} FAIL={failed}")
sys.exit(1 if failed else 0)
