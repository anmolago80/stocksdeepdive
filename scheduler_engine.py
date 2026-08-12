"""
scheduler_engine.py

A tiny in-process scheduler for the two background jobs this site needs:

  1. NIGHTLY universe scans  -> nightly_scan.run_universe_scan(...)
  2. WEEKLY watchlist digest -> digest_engine.run_weekly_digest()

WHY IN-PROCESS, NOT A SEPARATE RAILWAY CRON SERVICE: Railway volumes
attach to exactly ONE service, and the web app needs the volume (for the
scan results, watchlists, and compounder data). Running the jobs on a
daemon thread inside the web process means one service, one volume, zero
coordination - the trade-off being that jobs only run while the web
service is up (which, on Railway, is all the time).

Started once per server process from app.py via st.cache_resource. All
work is wrapped in try/except - a failed scan or send logs and waits for
the next window, it never takes the site down. A state file on the data
dir records last-run dates so a redeploy mid-evening doesn't double-run.

CONFIG (Railway environment variables, all optional):
  SCHEDULER_ENABLED      - "false" disables everything (default: enabled).
  NIGHTLY_UNIVERSES      - comma-separated, default "ASX 200". These are
                           the universes pre-scanned overnight.
  NIGHTLY_SCAN_UTC_HOUR  - default 20 (= 6am Brisbane).
  DIGEST_UTC_WEEKDAY     - default 6 = Sunday (so ~7am Monday Brisbane).
  DIGEST_UTC_HOUR        - default 21.
"""

import json
import os
import threading
import time
from datetime import datetime, timezone


def _data_dir():
    return os.environ.get("RAILWAY_VOLUME_MOUNT_PATH") or os.path.dirname(__file__)


STATE_PATH = os.path.join(_data_dir(), "scheduler_state.json")
_CHECK_EVERY_SECONDS = 60


def _load_state():
    try:
        with open(STATE_PATH) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save_state(state):
    try:
        tmp = STATE_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(state, f)
        os.replace(tmp, STATE_PATH)
    except OSError:
        pass


def _cfg():
    return {
        "enabled": (os.environ.get("SCHEDULER_ENABLED", "true").strip().lower()
                    not in ("false", "0", "no", "off")),
        "universes": [u.strip() for u in
                      os.environ.get("NIGHTLY_UNIVERSES", "ASX 200").split(",")
                      if u.strip()],
        "scan_hour": int(os.environ.get("NIGHTLY_SCAN_UTC_HOUR", "20")),
        "digest_weekday": int(os.environ.get("DIGEST_UTC_WEEKDAY", "6")),
        "digest_hour": int(os.environ.get("DIGEST_UTC_HOUR", "21")),
    }


def _run_nightly(cfg, log):
    import nightly_scan
    for universe in cfg["universes"]:
        try:
            nightly_scan.run_universe_scan(universe, log=log)
        except Exception as e:
            log(f"[scheduler] nightly scan {universe} failed: {e}")


def _run_digest(log):
    try:
        import digest_engine
        digest_engine.run_weekly_digest(log=log)
    except Exception as e:
        log(f"[scheduler] weekly digest failed: {e}")


def _loop(log):
    while True:
        try:
            cfg = _cfg()
            if cfg["enabled"]:
                now = datetime.now(timezone.utc)
                today = now.strftime("%Y-%m-%d")
                state = _load_state()

                if now.hour >= cfg["scan_hour"] and state.get("last_scan_date") != today:
                    state["last_scan_date"] = today
                    _save_state(state)  # mark first: never double-start a 30-min job
                    log(f"[scheduler] starting nightly scans ({', '.join(cfg['universes'])})")
                    _run_nightly(cfg, log)

                if (now.weekday() == cfg["digest_weekday"]
                        and now.hour >= cfg["digest_hour"]
                        and state.get("last_digest_date") != today):
                    state = _load_state()
                    state["last_digest_date"] = today
                    _save_state(state)
                    log("[scheduler] starting weekly digest")
                    _run_digest(log)
        except Exception as e:
            log(f"[scheduler] loop error: {e}")
        time.sleep(_CHECK_EVERY_SECONDS)


def start(log=print):
    """Start the scheduler daemon thread (idempotent per process via
    app.py's st.cache_resource). Returns the thread."""
    t = threading.Thread(target=_loop, args=(log,), daemon=True,
                         name="sdd-scheduler")
    t.start()
    return t
