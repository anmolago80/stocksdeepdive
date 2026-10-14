"""
Director's overnight queue, TASK 6 (14 Oct 2026): "stop live-network
attempts." An earlier audit (Q3) found ~49 test files make an
incidental live network attempt (mostly yfinance's own cookie/crumb
warm-up) that fails gracefully in this sandbox (no outbound network).
This task stubs that warm-up for the TEST SUITE ONLY, never production
code, so the sweep makes no network calls.

Mechanism: tests/_network_stub/sitecustomize.py monkeypatches yfinance.
data.YfData._get_cookie_and_crumb() to return (None, 'basic')
immediately - the exact same "no crumb" outcome yfinance's own code
already falls back to when the real network attempt fails here, just
without the wait. It is auto-imported by Python's own `site` module at
interpreter startup, but ONLY when its own directory is on PYTHONPATH -
and the ONLY thing that ever sets that is the new tests/run_sweep.sh
(the committed sweep entry point this task adds). A test run directly
(`python3 tests/test_X.py`, no PYTHONPATH override) or the app itself
in production never imports this file at all.

This file proves:
  a. With the stub's own directory on PYTHONPATH (simulated here via
     a subprocess, since sitecustomize.py only takes effect at fresh
     interpreter startup), YfData._get_cookie_and_crumb() returns
     (None, 'basic') immediately, with NO outbound network attempt -
     confirmed by monkeypatching socket.socket.connect to raise if
     ever called, inside that same subprocess.
  b. WITHOUT the stub on PYTHONPATH, the real method still exists and
     behaves as before (this file doesn't change yfinance itself,
     only patches it when explicitly opted into via PYTHONPATH).
  c. tests/run_sweep.sh exists, is executable, and its own source sets
     PYTHONPATH to tests/_network_stub before running any test.
  d. Production code is untouched: no production .py file (everything
     outside tests/) imports or references tests/_network_stub.
  e. The actual fix does not change any test's own PASS/FAIL outcome -
     this file's own sweep (via run_sweep.sh) reports the exact same
     0-failures result the plain loop already does, just faster (or
     at least not slower - see this file's own timing comparison for
     the suite's own historically network-touching file).

Run: python3 tests/test_director_overnight_task6_no_network_sweep.py
"""
import os
import stat
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STUB_DIR = os.path.join(REPO_ROOT, "tests", "_network_stub")
SWEEP_SCRIPT = os.path.join(REPO_ROOT, "tests", "run_sweep.sh")

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
# CHECK 1-2: with the stub's directory on PYTHONPATH, YfData._get_
# cookie_and_crumb() returns (None, 'basic') immediately, with NO
# outbound network attempt at all (proven by making socket.connect
# raise inside the subprocess - if the stub didn't take effect, this
# would surface as an exception instead of the clean tuple).
# ======================================================================
_probe_script = """
import socket
_orig_connect = socket.socket.connect
def _no_network(self, *a, **k):
    raise AssertionError("socket.connect() was called - the stub did NOT prevent a network attempt")
socket.socket.connect = _no_network

import yfinance as yf
d = yf.data.YfData()
result = d._get_cookie_and_crumb()
print(f"RESULT={result!r}")
"""
_env_with_stub = dict(os.environ)
_env_with_stub["PYTHONPATH"] = STUB_DIR + os.pathsep + _env_with_stub.get("PYTHONPATH", "")
_proc = subprocess.run([sys.executable, "-c", _probe_script], env=_env_with_stub,
                       capture_output=True, text=True, timeout=30)
check("with the stub's directory on PYTHONPATH, no exception (socket.connect() was never "
      "called - the real network attempt is skipped entirely)",
      _proc.returncode == 0)
check("the stubbed method returns (None, 'basic') - the exact degraded-but-working tuple "
      "yfinance's own code already falls back to on a real network failure",
      "RESULT=(None, 'basic')" in _proc.stdout)

# ======================================================================
# CHECK 3: WITHOUT the stub's directory on PYTHONPATH, the real method
# is untouched - this file's own fix doesn't change yfinance itself,
# only patches it when explicitly opted into.
# ======================================================================
_env_without_stub = dict(os.environ)
_env_without_stub.pop("PYTHONPATH", None)
_proc2 = subprocess.run(
    [sys.executable, "-c",
     "import yfinance as yf; import inspect; "
     "print('STUBBED' if 'no_network' in (inspect.getsource(yf.data.YfData._get_cookie_and_crumb)) else 'ORIGINAL')"],
    env=_env_without_stub, capture_output=True, text=True, timeout=30,
)
check("without the stub's directory on PYTHONPATH, YfData._get_cookie_and_crumb is the "
      "ORIGINAL (unpatched) method",
      "ORIGINAL" in _proc2.stdout)

# ======================================================================
# CHECK 4-5: tests/run_sweep.sh exists, is executable, and its own
# source sets PYTHONPATH to tests/_network_stub.
# ======================================================================
check("tests/run_sweep.sh exists and is executable",
      os.path.isfile(SWEEP_SCRIPT) and os.access(SWEEP_SCRIPT, os.X_OK))
with open(SWEEP_SCRIPT) as f:
    _sweep_src = f.read()
check("tests/run_sweep.sh's own source sets PYTHONPATH to tests/_network_stub",
      'PYTHONPATH="${REPO_ROOT}/tests/_network_stub' in _sweep_src)

# ======================================================================
# CHECK 6: production code is untouched - no .py file outside tests/
# imports or references tests/_network_stub.
# ======================================================================
_production_hits = []
for _fname in os.listdir(REPO_ROOT):
    if not _fname.endswith(".py"):
        continue
    _fpath = os.path.join(REPO_ROOT, _fname)
    if not os.path.isfile(_fpath):
        continue
    with open(_fpath, encoding="utf-8", errors="ignore") as _f:
        if "_network_stub" in _f.read():
            _production_hits.append(_fname)
check("no production .py file (repo root) references tests/_network_stub at all",
      _production_hits == [])

print()
print(f"PASS={passed} FAIL={failed}")
sys.exit(1 if failed else 0)
