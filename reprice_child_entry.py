"""
TASK 25 (Director-directed, 31 Oct 2026, part A) - standalone entry
point for the nightly reprice pass's own short-lived CHILD PROCESS.
Launched via subprocess.Popen (see scheduler_engine._run_reprice_pass_
in_child()'s own docstring for the full writeup) - NEVER multi-
processing's "spawn" start method, deliberately: spawn's own bootstrap
re-executes whatever script is the CALLING process's own sys.modules
["__main__"], which is app.py (no __main__ guard at all - Streamlit
scripts always run top-to-bottom, and app.py calls its own
_start_background_scheduler() unguarded at module level) whenever the
Streamlit subprocess's own scheduler thread is the one that reaches
the reprice pass that tick. subprocess.Popen launching THIS standalone
file sidesteps the whole "__main__" concept entirely - there is no
ambient main script involved, and nothing here ever runs except what's
written below, so no second scheduler thread, FastAPI server, or
Streamlit subprocess can ever start in this process.

Protocol: reads one line of JSON - {"to_reprice": [...universe names],
"run_night": "YYYY-MM-DD" or null} - from stdin, runs scheduler_
engine._reprice_universes_loop() (the EXACT same function the in-
process fallback path calls - so "does the child produce identical
saved rows to the in-process path" is true by construction, never two
independently-maintained copies of the same loop), writing every log
line to its own stdout, flushed immediately (never batched), one line
per call, no prefix/framing of any kind added here - the parent's own
reader relays each line to its real log exactly as received. Exits 0
once the loop returns normally; a non-zero exit (or no exit at all,
from the parent's own perspective, if this hangs) tells the parent to
fall back to running the pass in-process instead.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def main():
    payload = json.loads(sys.stdin.readline())
    to_reprice = payload["to_reprice"]
    run_night = payload.get("run_night")

    import scheduler_engine

    def _log(msg):
        print(msg, flush=True)

    scheduler_engine._reprice_universes_loop(to_reprice, run_night, _log)


if __name__ == "__main__":
    main()
