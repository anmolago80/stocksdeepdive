"""
TASK 56 (Director-directed, 13 Nov 2026) - standalone entry point for
the one-off Quality v2 refresh's own short-lived CHILD PROCESS.
Launched via subprocess.Popen (see scheduler_engine._run_quality_v2_
refresh_pass_in_child()'s own docstring for the full writeup) - the
SAME "never multiprocessing's spawn" reasoning as reprice_child_
entry.py's own module docstring applies here unchanged (see that
file): subprocess.Popen launching THIS standalone file sidesteps the
"__main__" bootstrap problem entirely, so no second scheduler thread,
FastAPI server, or Streamlit subprocess can ever start in this
process.

Protocol: no input expected on stdin (unlike reprice_child_entry.py,
this always covers EVERY stored universe - "all stored rows" per the
Director's own instruction, never a subset to hand off) - starts the
instant it's launched. Runs nightly_scan.run_quality_v2_refresh_pass()
(the EXACT same function the in-process fallback path calls - "does
the child produce identical results to the in-process path" is true
by construction), writing every log line to its own stdout, flushed
immediately, one line per call, no prefix/framing added here - the
parent's own reader relays each line to its real log exactly as
received. Exits 0 once the pass returns normally; a non-zero exit (or
no exit at all, from the parent's own perspective, if this hangs)
tells the parent to fall back to running the pass in-process instead.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def main():
    import nightly_scan

    def _log(msg):
        print(msg, flush=True)

    nightly_scan.run_quality_v2_refresh_pass(log=_log)


if __name__ == "__main__":
    main()
