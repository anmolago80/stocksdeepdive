"""
scheduler_engine.py

A tiny in-process scheduler for the background jobs this site needs:

  1. NIGHTLY universe scans   -> nightly_scan.run_universe_scan(...)
  2. WEEKLY watchlist digest  -> weekly_brief_engine.run_weekly_brief()
                                 (AI-readiness roadmap Phase 8: the same
                                 Sunday send slot digest_engine.py always
                                 used, now sent by weekly_brief_engine.py
                                 instead - see that module's own docstring
                                 for why it's a new module rather than an
                                 edit to digest_engine.py, which stays
                                 untouched and unused by the live schedule)
  3. NIGHTLY portfolio watchdog -> portfolio_watchdog_engine.run_nightly_watchdog()
  4. WEEKLY earnings-calendar refresh -> results_engine.refresh_earnings_calendar()
                                 (Services batch, Part 4: results-day
                                 re-analysis. Separate weekday/hour from
                                 the digest so the two weekly jobs don't
                                 compete for the same lock window.)
  5. NIGHTLY results-day check -> results_engine.check_results_day()
                                 (Services batch, Part 4 - runs every
                                 night as part of _run_nightly() below,
                                 same as the alert checks; cheap even on
                                 nights nothing reported.)

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
  NIGHTLY_UNIVERSES      - comma-separated "name" or "name:cadence" items
                           (Fix 8b, AI fixes round 2, 2026-08-31) - these
                           are the universes pre-scanned overnight. A
                           plain "name" (no colon) defaults to cadence
                           "daily", for back-compat with every value this
                           variable has ever held before this fix.
                           cadence is one of:
                             daily          - due whenever its saved scan
                                              is >20h old (as before).
                             weekly         - due whenever its saved scan
                                              is >6 days old, on whatever
                                              night that falls.
                             mon/tue/wed/thu/fri/sat/sun - due (>6 days
                                              old) ONLY on that UTC
                                              weekday - this is what
                                              actually spreads several
                                              large weekly universes
                                              across separate nights
                                              instead of letting them all
                                              go stale together and pile
                                              onto one. See
                                              _universes_needing_scan()'s
                                              own docstring.
                           Include "imported" to also work through the
                           TradingView CSV import queue (screen_import_
                           store.py) - it always runs last, after every
                           real index universe due that night, regardless
                           of where it sits in this list. Defaults to
                           _DEFAULT_NIGHTLY_UNIVERSES below - the round 2
                           instruction doc's own recommended cadence line
                           (daily: ASX 200/S&P 500/Nasdaq 100/Dow Jones 30/
                           ASX 300/ASX All Technology; one large weekly
                           universe per weekday Mon-Fri: All Ordinaries/
                           S&P 400 MidCap/Small Caps (S&P 600)/Russell
                           1000/Russell 2000) - set as the code default so
                           broader coverage works without the owner
                           needing to touch Railway at all; still
                           override-able there if the first live run's
                           actual per-universe minutes (see 8b's own
                           verify step) says the split needs adjusting.
                           Derived universes (ASX Small Ordinaries/ASX
                           100/ASX 50/S&P 1500) are NOT listed here and
                           need no scan slot of their own - Fix 8c below
                           builds their scan tables/snapshots by
                           filtering an already-scanned parent universe.
  NIGHTLY_SCAN_UTC_HOUR  - default 20 (= 6am Brisbane).
  DIGEST_UTC_WEEKDAY     - default 6 = Sunday (so ~7am Monday Brisbane).
  DIGEST_UTC_HOUR        - default 21.
  WATCHDOG_UTC_HOUR      - default 22. AI-readiness roadmap Phase 5: the
                           Portfolio AI watchdog (portfolio_watchdog_engine.
                           py) - runs nightly (every day, unlike the
                           weekly digest), scheduled after the nightly
                           scan hour so that night's scan data is fresh
                           when the watchdog reads it.
  EARNINGS_REFRESH_UTC_WEEKDAY - default 2 = Wednesday. Services batch
                           Part 4: which day the earnings-calendar
                           refresh runs - deliberately not the same day
                           as the weekly digest (Sunday), so the two
                           weekly jobs never compete for the same lock.
  EARNINGS_REFRESH_UTC_HOUR    - default 19 (before the nightly scan
                           hour, so a ticker whose earnings date changed
                           this week is picked up before that night's
                           results-day check runs).
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


# Fix 8b, AI fixes round 2 (2026-08-31): the round 2 instruction doc's
# own recommended default cadence line, set as the code default so
# broader coverage works out of the box without the owner touching
# Railway - see NIGHTLY_UNIVERSES' own docstring above for what each
# cadence means. Daily: the six smaller/higher-priority universes.
# One large weekly universe per weekday Mon-Fri, spreading them across
# separate nights rather than piling onto one.
_DEFAULT_NIGHTLY_UNIVERSES = (
    "ASX 200:daily, S&P 500:daily, Nasdaq 100:daily, Dow Jones 30:daily, "
    "ASX 300:daily, ASX All Technology:daily, All Ordinaries:mon, "
    "S&P 400 MidCap:tue, Small Caps (S&P 600):wed, Russell 1000:thu, "
    "Russell 2000:fri"
)

_WEEKDAY_ABBR = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}

# Rough constituent-count table, ONLY used to order tonight's due
# universes smallest-first (8b's own verify step: "order the nightly
# run smallest-first so the daily ones always finish") - not used for
# anything else, so an approximate/stale count here is harmless; an
# unlisted universe sorts last (safest assumption - never lets an
# unknown-size universe jump the queue ahead of a known-small one).
_APPROX_UNIVERSE_SIZE = {
    "ASX 20": 20, "Dow Jones 30": 30, "ASX 50": 50, "ASX All Technology": 60,
    "Nasdaq 100": 100, "ASX 100": 100, "ASX 200": 200,
    "ASX Small Ordinaries": 200, "ASX 300": 300, "S&P 400 MidCap": 400,
    "All Ordinaries": 500, "S&P 500": 500, "Small Caps (S&P 600)": 600,
    "Russell 1000": 1000, "S&P 1500": 1500, "Russell 2000": 2000,
}


def _parse_nightly_universes(raw):
    """Parses NIGHTLY_UNIVERSES' "name" or "name:cadence" syntax - see
    that env var's own docstring above for the full cadence vocabulary.
    A bare "name" (no colon) defaults to cadence "daily", for back-compat
    with every value this variable has ever held before Fix 8b. An
    unrecognised cadence also falls back to "daily" (never silently
    drops the universe entirely just because of a typo). Returns an
    ordered {universe_name: cadence} dict, insertion order preserved
    from `raw` (though _universes_needing_scan below re-sorts its own
    output smallest-first, so this function's order isn't load-bearing
    on its own)."""
    from collections import OrderedDict
    out = OrderedDict()
    for item in (raw or "").split(","):
        item = item.strip()
        if not item:
            continue
        if ":" in item:
            name, cadence = item.split(":", 1)
            name, cadence = name.strip(), cadence.strip().lower()
        else:
            name, cadence = item, "daily"
        if cadence not in ("daily", "weekly") and cadence not in _WEEKDAY_ABBR:
            cadence = "daily"
        if name:
            out[name] = cadence
    return out


def _cfg():
    cadence = _parse_nightly_universes(
        os.environ.get("NIGHTLY_UNIVERSES", _DEFAULT_NIGHTLY_UNIVERSES))
    return {
        "enabled": (os.environ.get("SCHEDULER_ENABLED", "true").strip().lower()
                    not in ("false", "0", "no", "off")),
        # Kept alongside "universe_cadence" (not derived on the fly at
        # every call site) since _run_nightly and other existing readers
        # of cfg["universes"] only ever need the plain name list.
        "universes": list(cadence.keys()),
        "universe_cadence": cadence,
        "scan_hour": int(os.environ.get("NIGHTLY_SCAN_UTC_HOUR", "20")),
        "digest_weekday": int(os.environ.get("DIGEST_UTC_WEEKDAY", "6")),
        "digest_hour": int(os.environ.get("DIGEST_UTC_HOUR", "21")),
        "watchdog_hour": int(os.environ.get("WATCHDOG_UTC_HOUR", "22")),
        "earnings_refresh_weekday": int(os.environ.get("EARNINGS_REFRESH_UTC_WEEKDAY", "2")),
        "earnings_refresh_hour": int(os.environ.get("EARNINGS_REFRESH_UTC_HOUR", "19")),
    }


# Audit fix 2.8: the "never double-start" guard in _loop() below (the
# state-file attempt counter) only protects one thread inside ONE process
# against itself - nothing stops a second Railway replica (if this
# service is ever scaled beyond the single-replica assumption this module
# is documented above to require) from independently deciding the same
# scan/digest is due and running it at the same time. This is currently
# safe only because nothing enforces single-replica in code. A file-based
# lock on the same Railway Volume every other persisted file in this app
# already relies on being shared/durable across replicas of one service
# gives real cross-process coordination without needing any Railway-
# platform-specific API: os.O_CREAT|O_EXCL is atomic at the filesystem
# level, so only one process can ever win the race to create the lock
# file. A lock older than _JOB_LOCK_STALE_SECONDS is treated as
# abandoned (from a process that crashed before releasing it) and
# cleared, so a dead lock can't wedge every future run forever.
_JOB_LOCK_STALE_SECONDS = 3 * 3600


def _lock_path(job_name):
    base = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH") or os.path.dirname(__file__)
    return os.path.join(base, f"scheduler_{job_name}.lock")


def _acquire_job_lock(job_name):
    """True if this process just acquired the lock for `job_name` (caller
    must call _release_job_lock when done); False if another process
    already holds it. Fails OPEN (returns True without a real lock) on any
    filesystem error, matching this module's existing single-replica
    fail-safe stance - a lock that can't be checked should never be the
    reason the scheduler stops running altogether."""
    path = _lock_path(job_name)
    try:
        if os.path.exists(path):
            age = time.time() - os.path.getmtime(path)
            if age > _JOB_LOCK_STALE_SECONDS:
                try:
                    os.remove(path)
                except OSError:
                    pass
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        with os.fdopen(fd, "w") as f:
            f.write(f"pid={os.getpid()} started={datetime.now(timezone.utc).isoformat()}\n")
        return True
    except FileExistsError:
        return False
    except OSError:
        return True


def _release_job_lock(job_name):
    try:
        os.remove(_lock_path(job_name))
    except OSError:
        pass


def _run_nightly(cfg, log):
    import nightly_scan

    # Services batch, Part 1 (metric alerts): snapshot every alerted
    # ticker's LAST recorded value before any of tonight's scans touch
    # score_history - see alert_engine.py's module docstring for exactly
    # why this has to happen up front rather than inside the per-universe
    # loop below. A failure here degrades to "no crossing-detection
    # tonight" (>=/<=/becomes alerts still work fine), never blocks the
    # scan itself.
    try:
        import alert_engine
        alert_prev_map = alert_engine.snapshot_previous_values(log=log)
    except Exception as e:
        log(f"[scheduler] alert prev-value snapshot failed: {e}")
        alert_prev_map = {}

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
                payload = nightly_scan.run_imported_scan(log=log)
            else:
                payload = nightly_scan.run_universe_scan(universe, log=log)
            # AI-readiness Phase 1 (AI_ROADMAP_stocksdeepdive.md): build the
            # public /s/<TICKER> snapshot + /api/v1 data for this universe
            # right after its scan lands - re-shapes rows the scan just
            # computed, no extra network calls. A failure here must never
            # take down the scan it rides on.
            if payload and payload.get("rows"):
                try:
                    import snapshot_store
                    snapshot_store.build_snapshots_from_scan(
                        universe, payload["rows"], log=log)
                except Exception as e:
                    log(f"[scheduler] snapshot build {universe} failed: {e}")
                # Services batch, Part 1: evaluate this universe's alerts
                # right away (queues hits only - nothing is emailed/pushed
                # here, see the batched send after the loop below).
                try:
                    import alert_engine
                    alert_engine.check_universe_rows(payload["rows"], alert_prev_map, log=log)
                except Exception as e:
                    log(f"[scheduler] alert check {universe} failed: {e}")
                # Services batch, Part 2: refresh insider/buyback data for
                # up to INSIDER_NIGHTLY_CAP stale tickers from this
                # universe - see insider_engine.py's own module docstring
                # for why this is capped per universe rather than covering
                # every scanned ticker every night.
                try:
                    import insider_engine
                    insider_engine.refresh_universe(payload["rows"], log=log)
                except Exception as e:
                    log(f"[scheduler] insider refresh {universe} failed: {e}")
        except Exception as e:
            log(f"[scheduler] nightly scan {universe} failed: {e}")

    # Services batch, Part 1: tickers with an active alert that weren't
    # covered by any universe/imported scan above get one lightweight
    # snapshot each, then a single batched email+push covering every hit
    # queued tonight (from the loop above AND this extra pass).
    try:
        import alert_engine
        alert_engine.run_extra_ticker_pass(alert_prev_map, log=log)
    except Exception as e:
        log(f"[scheduler] alert extra pass failed: {e}")
    try:
        import alert_engine
        alert_engine.send_batched_notifications(log=log)
    except Exception as e:
        log(f"[scheduler] alert notifications failed: {e}")

    # Services batch, Part 4: results-day re-analysis. Runs every night
    # (cheap - a small local table scan, one live re-score only for a
    # ticker that genuinely reported 1 or 3 days ago) - see
    # results_engine.check_results_day()'s own docstring for the day+1/
    # day+3 pass logic and its idempotency guarantees.
    try:
        import results_engine
        results_engine.check_results_day(log=log)
    except Exception as e:
        log(f"[scheduler] results-day check failed: {e}")

    # Fix 8c, AI fixes round 2 (2026-08-31) - see _build_derived_
    # universes()'s own docstring above.
    try:
        _build_derived_universes(log)
    except Exception as e:
        log(f"[scheduler] derived universes build failed: {e}")


# Fix 8c, AI fixes round 2 (2026-08-31): derived universes (no scan slot
# of their own - see NIGHTLY_UNIVERSES' own docstring above) get their
# scan_store/snapshot_store entries built by filtering an already-
# scanned parent universe's rows down to the correct membership
# (scanner_engine.get_universe_pool() - the exact same live-fetch-with-
# fallback logic the Scanner UI's own "Run Scan" button already uses
# for these universes), rather than a second independent nightly scan.
# Genuinely free: no new network call, no new yfinance/quality/moat
# computation - every row here was already computed scanning the
# parent(s). Called unconditionally at the end of _run_nightly() below
# (not gated on which real universes were due tonight) so a derived
# universe never silently goes stale past scan_store.load_scan()'s own
# 72h cutoff on a night none of its parents happened to need rescanning.
_DERIVED_UNIVERSE_PARENTS = {
    "ASX Small Ordinaries": ["ASX 300", "ASX 200"],
    "ASX 100": ["ASX 200", "ASX 300"],
    "ASX 50": ["ASX 200", "ASX 300"],
    "ASX 20": ["ASX 200", "ASX 300"],
    "S&P 1500": ["S&P 500", "S&P 400 MidCap", "Small Caps (S&P 600)"],
}


def _build_derived_universes(log):
    import scan_store
    import scanner_engine
    import snapshot_store

    for universe, parents in _DERIVED_UNIVERSE_PARENTS.items():
        try:
            country = ("Australia" if universe in scanner_engine.AUSTRALIA_UNIVERSES
                       else "USA")
            pool_df, source = scanner_engine.get_universe_pool(country, universe)
            if pool_df is None or pool_df.empty:
                log(f"[scheduler] derived universe {universe}: no membership "
                    f"resolved ({source}), skipping")
                continue
            wanted = set(pool_df["Ticker"])

            row_by_ticker = {}
            for parent in parents:
                payload = scan_store.load_scan(parent)
                if not payload:
                    continue
                for row in payload.get("rows") or []:
                    t = (row.get("Ticker") or "").strip().upper()
                    if t and t not in row_by_ticker:
                        row_by_ticker[t] = row

            rows = [row_by_ticker[t] for t in sorted(wanted) if t in row_by_ticker]
            if not rows:
                log(f"[scheduler] derived universe {universe}: none of its "
                    f"{len(wanted)} members have a scanned parent row yet, skipping")
                continue
            rows.sort(key=lambda r: r.get("Long Score") or 0, reverse=True)

            scan_store.save_scan(universe, rows, f"Derived from {'/'.join(parents)} ({source})")
            snapshot_store.build_snapshots_from_scan(universe, rows, log=log)
            log(f"[scheduler] derived universe {universe}: {len(rows)}/{len(wanted)} "
                f"members covered from {'/'.join(parents)}")
        except Exception as e:
            log(f"[scheduler] derived universe {universe} failed: {e}")


def _run_digest(log):
    """AI-readiness roadmap Phase 8: this job slot now sends
    weekly_brief_engine's personalised AI brief rather than
    digest_engine's plain table - see weekly_brief_engine.py's own
    docstring. Kept the name _run_digest / the "digest" lock and state
    keys below unchanged (same weekday/hour config, same job-lock
    discipline) since only the content generator changed, not the
    schedule or the single-process coordination around it."""
    try:
        import weekly_brief_engine
        weekly_brief_engine.run_weekly_brief(log=log)
    except Exception as e:
        log(f"[scheduler] weekly brief failed: {e}")


def _run_watchdog(log):
    """AI-readiness roadmap Phase 5: the nightly Portfolio AI watchdog -
    see portfolio_watchdog_engine.py's own docstring for what it does and
    why. Same shape as _run_digest above: import deferred (a background
    job's heavy imports shouldn't slow every other scheduler tick), the
    whole run wrapped so a failure here logs and waits for tomorrow night
    rather than ever taking the scheduler thread down."""
    try:
        import portfolio_watchdog_engine
        portfolio_watchdog_engine.run_nightly_watchdog(log=log)
    except Exception as e:
        log(f"[scheduler] portfolio watchdog failed: {e}")


def _run_earnings_refresh(log):
    """Services batch, Part 4, WEEKLY job: refresh the earnings calendar
    for every ticker this site has ever scanned or that anyone follows -
    see results_engine.refresh_earnings_calendar()'s own docstring for
    what "refresh" means and why it's capped per run. Deferred imports,
    same shape as _run_digest/_run_watchdog above.

    Services batch 2, Part 4 (2026-09-01): widened to also cover every
    ticker in anyone's watchlist (watchlist_store), portfolio
    (portfolio_store.all_portfolio_tickers), or with an active alert
    (alert_store.tickers_with_active_alerts) - the results calendar
    (calendar_render.py) is only as complete as this watch list, and
    those three sources previously fell outside it (only score_history's
    "ever scanned" set and follow_store's separate "follow this
    company's research" list were unioned in). score_history.
    all_tracked_tickers() already covers "every ticker in the daily
    universes" for free - it's every ticker any nightly universe scan
    has EVER written a row for, which is every ticker that's a member of
    a universe this site actually scans nightly, cumulatively - so no
    separate hardcoded ASX 200/300 + S&P 500 + Nasdaq 100 + Dow 30 + All
    Tech list is needed here (that would just be a second, driftable
    copy of scanner_engine.py's own universe list)."""
    try:
        import score_history
        import follow_store
        import watchlist_store
        import portfolio_store
        import alert_store
        import results_engine
        tickers = set(score_history.all_tracked_tickers())
        tickers.update(follow_store.all_followed_tickers())
        for _email, _wl_tickers in watchlist_store.all_users():
            tickers.update(_wl_tickers)
        tickers.update(portfolio_store.all_portfolio_tickers())
        tickers.update(alert_store.tickers_with_active_alerts())
        tickers = sorted(t for t in tickers if t)
        if not tickers:
            log("[scheduler] earnings calendar refresh: no tickers to watch yet")
            return
        results_engine.refresh_earnings_calendar(tickers, log=log)
    except Exception as e:
        log(f"[scheduler] earnings calendar refresh failed: {e}")


def _universes_needing_scan(cfg):
    """Universes whose SAVED scan is missing or stale - the source of
    truth is the result file, not a 'ran today' marker, so a deploy/
    restart that kills a scan mid-run self-heals on the next check
    instead of silently skipping a whole day.

    Fix 8b, AI fixes round 2 (2026-08-31): staleness threshold now
    depends on the universe's own cadence (cfg["universe_cadence"]) -
    "daily" keeps the original >20h threshold; "weekly" and a specific
    weekday (mon..sun) both use >6 days (144h), since neither is meant
    to re-scan every night. A weekday-pinned universe is additionally
    only ever considered due on ITS OWN weekday (UTC) - that's what
    actually spreads several large weekly universes across separate
    nights instead of letting them all go stale together in the same
    ~6-day window and pile onto one night; a bare "weekly" (no pinned
    day) has no such restriction - simply due whenever its 6 days are
    up, on whichever night that falls.

    Result is sorted smallest-first (8b's own verify step) using the
    rough _APPROX_UNIVERSE_SIZE table above, so on a night with a mix
    of small daily and one large weekly universe due, the small ones
    finish first even if the run gets cut short."""
    import scan_store
    today_weekday = datetime.now(timezone.utc).weekday()  # Monday=0..Sunday=6
    due = []
    for u, cadence in cfg["universe_cadence"].items():
        if cadence in _WEEKDAY_ABBR and _WEEKDAY_ABBR[cadence] != today_weekday:
            continue  # not this universe's night
        threshold_hours = 20 if cadence == "daily" else 144
        payload = scan_store.load_scan(u)
        if payload is None or payload.get("age_hours", 999) > threshold_hours:
            due.append(u)
    due.sort(key=lambda u: _APPROX_UNIVERSE_SIZE.get(u, 9999))
    return due


# Audit fix 2.10: the scheduler thread is memoized once via
# st.cache_resource (app.py); if it ever dies (an unusual exception type
# escaping the try/except in _loop() below, or the thread simply never
# scheduled by the interpreter for some reason), nightly scans and the
# weekly digest silently stop until the next full redeploy, with nothing
# surfacing that it's dead. This module-level timestamp is updated once
# per loop tick (whether or not there was anything to do that tick) so
# any caller can answer "is the loop still alive" without needing access
# to the thread object itself - see heartbeat_age_seconds() below.
# Process-local (a plain module global, not persisted) is intentional:
# the loop only ever runs in the same process that's asking, so there's
# nothing to gain from persisting it across a restart.
_last_heartbeat = None


def heartbeat_age_seconds():
    """Seconds since the scheduler loop last ticked, or None if it has
    never ticked in this process (start() hasn't been called yet, or the
    process is too young for the thread to have run its first iteration).
    Ticks every _CHECK_EVERY_SECONDS regardless of SCHEDULER_ENABLED - the
    loop itself keeps running even when disabled, it just skips doing any
    work - so this correctly reflects "is the thread alive", not "is a
    job currently due". A healthy scheduler updates this every
    _CHECK_EVERY_SECONDS; a stuck/dead thread shows a growing age with no
    ceiling. Exposed for an admin-only diagnostic (app.py's Stats
    popover) - deliberately not a public route, since a health-check
    endpoint answering "is the background job thread alive" is himself
    the kind of internal-state a public FastAPI route (server.py) has no
    reason to expose."""
    if _last_heartbeat is None:
        return None
    return time.time() - _last_heartbeat


def _loop(log):
    global _last_heartbeat
    while True:
        _last_heartbeat = time.time()
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
                        # Audit fix 2.8: cross-process lock, on top of the
                        # in-process state-file guard above - see
                        # _acquire_job_lock's docstring.
                        if _acquire_job_lock("nightly"):
                            try:
                                log(f"[scheduler] starting nightly scans ({', '.join(due)}) "
                                    f"[attempt {n_today + 1}/3 today]")
                                _run_nightly({**cfg, "universes": due}, log)
                            finally:
                                _release_job_lock("nightly")
                        else:
                            log("[scheduler] nightly scan skipped - another process "
                                "already holds the lock")

                # AI-readiness roadmap Phase 5: nightly (every day, unlike
                # the weekly digest below), one calendar-day-per-run guard
                # exactly like the nightly scan's own state-then-lock
                # pattern above.
                if (now.hour >= cfg["watchdog_hour"]
                        and state.get("last_watchdog_date") != today):
                    state = _load_state()
                    state["last_watchdog_date"] = today
                    _save_state(state)
                    if _acquire_job_lock("watchdog"):
                        try:
                            log("[scheduler] starting portfolio watchdog")
                            _run_watchdog(log)
                        finally:
                            _release_job_lock("watchdog")
                    else:
                        log("[scheduler] portfolio watchdog skipped - another process "
                            "already holds the lock")

                # Services batch, Part 4: earnings-calendar refresh -
                # WEEKLY, same one-day-per-run-per-weekday guard as the
                # digest below, deliberately on its own weekday/hour so
                # the two weekly jobs never compete for the same lock.
                if (now.weekday() == cfg["earnings_refresh_weekday"]
                        and now.hour >= cfg["earnings_refresh_hour"]
                        and state.get("last_earnings_refresh_date") != today):
                    state = _load_state()
                    state["last_earnings_refresh_date"] = today
                    _save_state(state)
                    if _acquire_job_lock("earnings_refresh"):
                        try:
                            log("[scheduler] starting earnings calendar refresh")
                            _run_earnings_refresh(log)
                        finally:
                            _release_job_lock("earnings_refresh")
                    else:
                        log("[scheduler] earnings calendar refresh skipped - another "
                            "process already holds the lock")

                # DIGEST_FORCE: set this variable to any NEW value (e.g.
                # "test1") to send the digest immediately, once per value -
                # the easy way to test a layout change without touching the
                # weekday/hour variables. Delete it (or leave it; it only
                # fires again when the value CHANGES) afterwards.
                force = os.environ.get("DIGEST_FORCE", "").strip()
                if force and state.get("digest_force_done") != force:
                    state["digest_force_done"] = force
                    _save_state(state)
                    if _acquire_job_lock("digest"):
                        try:
                            log(f"[scheduler] starting weekly digest (forced: {force})")
                            _run_digest(log)
                        finally:
                            _release_job_lock("digest")
                    else:
                        log("[scheduler] forced digest skipped - another process "
                            "already holds the lock")
                elif (now.weekday() == cfg["digest_weekday"]
                        and now.hour >= cfg["digest_hour"]
                        and state.get("last_digest_date") != today):
                    state = _load_state()
                    state["last_digest_date"] = today
                    _save_state(state)
                    if _acquire_job_lock("digest"):
                        try:
                            log("[scheduler] starting weekly digest")
                            _run_digest(log)
                        finally:
                            _release_job_lock("digest")
                    else:
                        log("[scheduler] weekly digest skipped - another process "
                            "already holds the lock")
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
