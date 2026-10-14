#!/bin/bash
# Full regression sweep - runs every tests/test_*.py file as its own
# process (same convention as every test file's own "Run: python3
# tests/test_X.py" docstring) and reports a pass/fail summary.
#
# Director's overnight queue TASK 6 (14 Oct 2026, "stop live-network
# attempts"): sets PYTHONPATH to tests/_network_stub so its own
# sitecustomize.py is auto-imported by every test process, stubbing
# yfinance's cookie/crumb warm-up (see that file's own docstring) -
# the sweep makes no outbound network attempt at all. This is the
# ONLY place that PYTHONPATH is ever set; running a test directly
# (`python3 tests/test_X.py`) or the app itself is unaffected.
#
# Usage: tests/run_sweep.sh [SWEEPDIR]
#   SWEEPDIR defaults to a fresh directory under /tmp. Per-test logs
#   and a summary.txt ("<exit code> <test name>" per line) are written
#   there; the script's own exit code is 0 iff every test exited 0.
set -u
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SWEEPDIR="${1:-$(mktemp -d /tmp/sdd_sweep.XXXXXX)}"
mkdir -p "$SWEEPDIR"
rm -f "${SWEEPDIR:?}"/*.log "${SWEEPDIR:?}/summary.txt"

export PYTHONPATH="${REPO_ROOT}/tests/_network_stub${PYTHONPATH:+:${PYTHONPATH}}"

cd "$REPO_ROOT"
_failures=0
for f in tests/test_*.py; do
    base=$(basename "$f" .py)
    timeout 110 python3 "$f" > "$SWEEPDIR/$base.log" 2>&1
    rc=$?
    echo "$rc $base" >> "$SWEEPDIR/summary.txt"
    [ "$rc" -ne 0 ] && _failures=$((_failures + 1))
done

echo "Sweep done: $SWEEPDIR"
echo "Failures: $_failures"
exit $([ "$_failures" -eq 0 ] && echo 0 || echo 1)
