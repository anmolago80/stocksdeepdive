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
                           the universes pre-scanned overnight. Include
                           "imported" to also work through the TradingView
                           CSV import queue (screen_import_store.py) - it
                           always runs last, after the real index
                           universes, however it's ordered in this list.
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
    # The "imported" virtual universe (screen_import_store's TradingView
    # CSV queue - see nightly_scan.run_imported_scan) always runs LAST,
    # after every configured index universe, regardless of where
    # "imported" sits in NIGHTLY_UNIVERSES - it's opportunistic overflow
    # work over an owner-curated watchlist, not something that should
    # delay the regular index scans everything else here depends on.
    ordered = (
        [u for u in cfg["universes"] if u != nightly_scan.IMPORTED_UNIVERSE]
        + [u for u in cfg["universes"] if u == nightly_scan.IMPORTED_UNIVERSE]
    )
    for universe in ordered:
        try:
            if universe == nightly_scan.IMPORTED_UNIVERSE:
                nightly_scan.run_imported_scan(log=log)
            else:
                nightly_scan.run_universe_scan(universe, log=log)
        except Exception as e:
            log(f"[scheduler] nightly scan {universe} failed: {e}")


def _run_digest(log):
    try:
        import digest_engine
        digest_engine.run_weekly_digest(log=log)
    except Exception as e:
        log(f"[scheduler] weekly digest failed: {e}")


def _universes_needing_scan(cfg):
    """Universes whose SAVED scan is missing or stale (>20h) - the source of
    truth is the result file, not a 'ran today' marker, so a deploy/restart
    that kills a scan mid-run self-heals on the next check instead of
    silently skipping a whole day."""
    import scan_store
    due = []
    for u in cfg["universes"]:
        payload = scan_store.load_scan(u)
        if payload is None or payload.get("age_hours", 999) > 20:
            due.append(u)
    return due


def _loop(log):
    while True:
        try:
            cfg = _cfg()
            if cfg["enabled"]:
                now = datetime.now(timezone.utc)
                today = now.strftime("%Y-%m-%d")
                state = _load_state()

                if now.hour >= cfg["scan_hour"]:
                    due = _universes_needing_scan(cfg)
                    attempts = state.get("scan_attempts", {})
                    n_today = attempts.get(today, 0)
                    if due and n_today < 3:  # retry cap: a persistently
                        # failing universe never turns into a hammering loop
                        state["scan_attempts"] = {today: n_today + 1}
                        state["last_scan_date"] = today
                        _save_state(state)  # mark first: never double-start
                        log(f"[scheduler] starting nightly scans ({', '.join(due)}) "
                            f"[attempt {n_today + 1}/3 today]")
                        _run_nightly({**cfg, "universes": due}, log)

                # DIGEST_FORCE: set this variable to any NEW value (e.g.
                # "test1") to send the digest immediately, once per value -
                # the easy way to test a layout change without touching the
                # weekday/hour variables. Delete it (or leave it; it only
                # fires again when the value CHANGES) afterwards.
                force = os.environ.get("DIGEST_FORCE", "").strip()
                if force and state.get("digest_force_done") != force:
                    state["digest_force_done"] = force
                    _save_state(state)
                    log(f"[scheduler] starting weekly digest (forced: {force})")
                    _run_digest(log)
                elif (now.weekday() == cfg["digest_weekday"]
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
