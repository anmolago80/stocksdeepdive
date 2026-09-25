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
  6. NIGHTLY off-site DB backup -> db_backup_engine.run_nightly_backup()
                                 (Mega-batch Part 10 - a consistent,
                                 gzipped copy of stocksdeepdive.db
                                 emailed to the owner via the existing
                                 Mailgun path. Scheduled after both the
                                 nightly scan AND the watchdog hour -
                                 see BACKUP_UTC_HOUR below - so it's
                                 backing up a night's data that's
                                 already fully settled, not racing it.)
  7. NIGHTLY volume monitor    -> volume_monitor.run_nightly_check()
                                 (Mega-batch Part 16 - checks the
                                 Volume's used/total bytes, monthly
                                 email alert at >=80% used, then runs
                                 bounded retention pruning. Its own hour
                                 (VOLUME_CHECK_UTC_HOUR), its own lock -
                                 see that constant's own docstring.)
  8. TWICE-DAILY quote sampler -> quote_recorder.run_asx_recorder()/
                                 run_us_recorder() (Trading Cost tab,
                                 Commit 1 - one real bid/ask quote per
                                 ticker per day, sampled while each
                                 market is genuinely open. The ONE job
                                 in this file that fires by LOCAL market
                                 time, not a UTC hour - see quote_
                                 recorder.is_due_now()'s own docstring
                                 for why every other job's UTC-hour
                                 check can't express that. Runs
                                 regardless of ENABLE_TRADING_COST; its
                                 own retention prune runs alongside the
                                 volume monitor above, in _run_volume_
                                 check().)

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
                           (daily: ASX 200/S&P 500/Nasdaq 100/Russell 2000/
                           ASX 300/ASX All Technology; one large weekly
                           universe per weekday Mon-Fri: All Ordinaries/
                           S&P 400 MidCap/Small Caps (S&P 600)/Russell
                           1000) - set as the code default so
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
  BACKUP_UTC_HOUR        - default 23 (Mega-batch Part 10) - after both
                           the nightly scan hour (20) and the watchdog
                           hour (22), so the backup captures a night
                           that's already fully done, not a half-
                           finished one.
  VOLUME_CHECK_UTC_HOUR  - default 23 (Mega-batch Part 16) - same hour
                           as the backup (its own separate job lock, so
                           the two don't collide); a disk-usage check +
                           retention prune doesn't need to run before or
                           after the backup specifically, just once
                           nightly after the day's writes have settled.
"""

import json
import os
import sys
import threading
import time
import traceback
import uuid
from datetime import datetime, timedelta, timezone

# Mega-batch Part 35.1: NIGHTLY JOBS table on the new owner Admin
# Dashboard - see _record_job() below for how each job's ok/warn/error
# result and duration are captured, and admin_metrics_store.py's own
# docstring for the rest of the site-pulse design.
try:
    import admin_metrics_store
except Exception:
    admin_metrics_store = None

# Mega-batch Part 36: the newsletter list's own nightly retention prune
# (newsletter_store.prune_stale_unconfirmed) - same guarded-import shape
# as admin_metrics_store above, wired into _run_volume_check() below.
try:
    import newsletter_store
except Exception:
    newsletter_store = None

# Trading Cost tab, Commit 1: imported at module level (not deferred
# inside a _run_* function like the other job modules) because _loop()
# below calls quote_recorder.is_due_now() on EVERY 60s tick, not once a
# day - that's a pure, cheap function with no network I/O of its own
# (importing the module itself doesn't touch yfinance's network layer,
# only `import`s the package), so there's no "don't slow every tick"
# cost to defer here. Same guarded-import shape as admin_metrics_store/
# newsletter_store above either way, so a broken quote_recorder.py can
# never take the whole scheduler down.
try:
    import quote_recorder
except Exception:
    quote_recorder = None

# Top 100 Commit 2 (25 Sep 2026, owner-reported): imported at module
# level, same reasoning as quote_recorder above - _loop() needs to
# cheaply check top100_store.get_batch_state() on every 60s tick (not
# once a day) to know whether the hourly poll block below has anything
# to do at all; a pure sqlite read, no network I/O, so no "don't slow
# every tick" cost to defer here.
try:
    import top100_store
except Exception:
    top100_store = None


def _data_dir():
    return os.environ.get("RAILWAY_VOLUME_MOUNT_PATH") or os.path.dirname(__file__)


STATE_PATH = os.path.join(_data_dir(), "scheduler_state.json")
_CHECK_EVERY_SECONDS = 60

# 18 Sep 2026 boot-time + retry fix: shared retry-cap for the daily jobs
# (backup, watchdog) that now persist their "done for today" guard only
# after a successful run - see the backup/watchdog blocks in _loop()
# below. Smaller than the nightly scan's own 3-attempt budget (that one
# covers an entire multi-universe scan that can genuinely take hours;
# these are single, fast, all-or-nothing jobs) - 2 is enough for "try
# again once" without letting a persistently broken night (e.g. Mailgun
# down for the backup job) hammer the lock every tick until midnight.
_DAILY_JOB_RETRY_CAP = 2


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
# One large weekly universe per weekday Mon-Thu, spreading them across
# separate nights rather than piling onto one.
#
# 9 Sep 2026 (owner-reported bug): Dow Jones 30 swapped out for Russell
# 2000, daily. fetch_dow30()'s Wikipedia scrape (added in this same Fix
# 8a/8b batch, flagged even then as never live-verified) turned out to
# never resolve any tickers in production - the current Wikipedia page
# for the Dow no longer carries a "Components" table matching the shape
# the scraper looks for, so this universe never got a first successful
# nightly scan and the Scanner showed nothing for it. Rather than chase
# Wikipedia's page structure (and risk hand-typing a stale/wrong 30-
# ticker fallback list into production), replaced it with Russell 2000 -
# already one of the more resilient fetchers here (iShares IWM holdings
# CSV, with its own last-good-list disk cache - see fetch_russell2000())
# - promoted from its old Friday-only slot to daily, taking over Dow 30's
# spot. scanner_engine.fetch_dow30()/DOW30_WIKI_URL/USA_UNIVERSES'
# dispatch branch are left in place, just unreferenced, in case Wikipedia's
# page structure gets fixed and someone wants to re-add it later.
#
# Part 34 ADDENDUM (owner-discussed, 11 Sep 2026): reworked into the
# owner's own explicit weekly calendar now that Dow 30 has a real dated
# static fallback behind its scrape attempt (scanner_engine.
# _DOW30_STATIC_FALLBACK - see that constant's own comment) - it can
# never come back empty again, so it reclaims a daily slot and Russell
# 2000 moves to Saturday (pairing with the addendum's Nasdaq Next Gen 100
# Saturday slot - SKIPPED here and everywhere else in this Part, since
# live verification found no Wikipedia source for it at all; see
# USA_UNIVERSES' own comment in scanner_engine.py). Daily core: ASX 200,
# ASX 300, ASX All Technology, S&P 500, Nasdaq 100, Dow Jones 30.
# Rotation: All Ordinaries:mon, S&P 400 MidCap:tue, Small Caps (S&P
# 600):wed, Russell 1000:thu, S&P 500 Dividend Aristocrats:fri, Russell
# 2000:sat. Sunday is left free (digest night), matching the addendum
# exactly. REMINDER (stated again in the Part 34 report): the owner must
# mirror this whole line into Railway's own NIGHTLY_UNIVERSES variable
# after deploy - this code default is not read at all once that env var
# is set.
_DEFAULT_NIGHTLY_UNIVERSES = (
    "ASX 200:daily, ASX 300:daily, ASX All Technology:daily, S&P 500:daily, "
    "Nasdaq 100:daily, Dow Jones 30:daily, All Ordinaries:mon, "
    "S&P 400 MidCap:tue, Small Caps (S&P 600):wed, Russell 1000:thu, "
    "S&P 500 Dividend Aristocrats:fri, Russell 2000:sat"
)

_WEEKDAY_ABBR = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}

# Rough constituent-count table, used to order tonight's due universes -
# not used for anything else, so an approximate/stale count here is
# harmless; an unlisted universe sorts last within its own group (safest
# assumption - never lets an unknown-size universe jump the queue ahead
# of a known-small one in that group).
#
# Commit H correction (20 Sep 2026): the ORIGINAL comment here read
# "order the nightly run smallest-first so the daily ones always
# finish" (8b's own verify step) - true as far as it went, but backwards
# for a weekday-pinned universe (mon..sun cadence). A daily universe
# missing a night gets another attempt in ~20h; a weekday-pinned one
# gets exactly one shot a week. Sorting purely smallest-first put every
# weekday-pinned universe LAST every single time (they're always the
# largest - see the table below), which combined with NIGHTLY_SCAN_UTC_
# HOUR starting only 4 hours before the UTC date boundary meant the one
# universe that could least afford to be interrupted or pushed past
# midnight was the one most likely to be. _scan_priority_key() below
# now sorts pinned universes AHEAD of dailies (smallest-first within
# each group still applies) - see its own docstring.
_APPROX_UNIVERSE_SIZE = {
    "ASX 20": 20, "Dow Jones 30": 30, "ASX 50": 50,
    "ASX Financials": 40, "ASX Materials & Mining": 45,
    "ASX Health Care": 15, "ASX Industrials": 30, "ASX A-REITs": 20,
    "ASX All Technology": 60, "ASX Consumer": 35,
    "US Energy": 25, "US Healthcare": 65, "US Industrials": 75,
    "US Financials": 70, "US Consumer": 90,
    "Nasdaq 100": 100, "ASX 100": 100, "US Technology": 100,
    "S&P 500 Dividend Aristocrats": 70, "ASX 200": 200,
    "ASX Small Ordinaries": 200, "ASX 300": 300, "S&P 400 MidCap": 400,
    "All Ordinaries": 500, "S&P 500": 500, "Small Caps (S&P 600)": 600,
    "Russell 1000": 1000, "S&P 1500": 1500, "Russell 2000": 2000,
    "Russell 3000": 3000,
}


def _scan_priority_key(u, cadence_map):
    """Sort key for tonight's due/missing universe list (Commit H): a
    weekday-pinned universe (cadence in _WEEKDAY_ABBR) sorts AHEAD of
    every daily/bare-weekly one, smallest-first within each group. A
    daily universe that misses tonight is due again in ~20h; a weekday-
    pinned one only comes due again in a week, so if tonight's run gets
    cut short (a restart, the catch-up window closing, the run simply
    taking longer than the hours left before midnight), it's the pinned
    universe that must go first - the dailies can afford to wait one
    more cycle, the pinned one effectively can't. Used by both
    _universes_needing_scan (the regular due-scan check) and
    _universes_missing_today (the catch-up check) so the same priority
    protects a truncated run either way."""
    is_pinned = cadence_map.get(u) in _WEEKDAY_ABBR
    return (0 if is_pinned else 1, _APPROX_UNIVERSE_SIZE.get(u, 9999))


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
        "backup_hour": int(os.environ.get("BACKUP_UTC_HOUR", "23")),
        "volume_check_hour": int(os.environ.get("VOLUME_CHECK_UTC_HOUR", "23")),
        # Top 100 tab, Commit 1: same "after the nightly scan" hour as
        # backup/volume_check above - own lock, so it never collides
        # with either even though all three default to the same hour.
        "top100_hour": int(os.environ.get("TOP100_UTC_HOUR", "23")),
    }


def nightly_universe_cadence():
    """{universe: cadence} ('daily' | 'weekly' | a weekday abbreviation),
    live-parsed from NIGHTLY_UNIVERSES (falling back to
    _DEFAULT_NIGHTLY_UNIVERSES when that env var is unset) - the public
    accessor _cfg()['universe_cadence'] already computes on every call,
    for a caller outside this module that only needs the cadence map and
    has no reason to reach into the private _cfg(). Added for the Admin
    Dashboard's weekly scan calendar (Part 53.1, app.py's
    _render_scan_calendar_html) - same "Scheduled" column source the
    scheduler's own due-scan check (_universes_needing_scan below) reads
    from."""
    return _cfg()["universe_cadence"]


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
# file.
#
# 18 Sep 2026 heartbeat fix (real incident): the ORIGINAL version of this
# lock only ever compared the lock FILE's mtime against a flat 3-hour
# ceiling, set once at creation and never refreshed while a job ran. The
# 17 Sep 21:05 UTC deploy killed the nightly scan mid-Russell-2000 (at
# 1000/1957) without ever reaching _release_job_lock's `finally`, and the
# stale-mtime reclaim that was supposed to clear an abandoned lock like
# that never fired in production - every catch-up tick since (confirmed
# in Railway logs, once a minute, across two more deploys) logged
# "another process already holds the lock" and skipped, meaning NO scan
# of any universe could run at all, catch-up or the regular 20:00 UTC
# nightly alike, until this fix ships. (The exact reason the mtime check
# never fired wasn't confirmed with certainty - a Railway Volume mount
# that resets file mtimes across a container replacement would produce
# precisely this symptom - but it doesn't matter: mtime is an OS-level
# property this code doesn't control end to end, so this fix stops
# relying on it entirely.)
#
# The lock file's own CONTENT now carries a heartbeat this process
# refreshes on every log line a running job emits (_record_job's
# _tracking_log wrapper below calls _refresh_job_lock_heartbeat() on
# every call - at least as often as the 25-ticker nightly_scan.py
# progress lines, usually far more often), plus a per-boot UUID
# identifying who holds it. Acquisition reclaims (deletes and
# recreates) a lock it finds when EITHER the heartbeat is older than
# _JOB_LOCK_HEARTBEAT_STALE_SECONDS regardless of holder, OR the
# holder's boot_id differs from this process's own AND the heartbeat is
# older than _JOB_LOCK_FOREIGN_BOOT_STALE_SECONDS (a quicker reclaim
# once we're sure it's some OTHER process's lock, not a stray re-entry
# of our own). A lock in the OLD plain-text "pid=... started=..."
# format (or any file that isn't valid JSON with both fields) has no
# heartbeat/holder to evaluate at all - _read_lock_payload treats that
# as unreadable, and _acquire_job_lock reclaims it immediately on the
# very first acquisition attempt after this fix ships, with no manual
# step. That is exactly the shape of the lock stuck in production
# right now, so deploying this fix is itself what clears the incident.
#
# URGENT Commit 3 (25 Sep 2026, owner-reported): the "quicker reclaim"
# comment two lines up turned out to have a real gap. This module is
# legitimately imported and its own scheduler thread started by BOTH
# server.py's FastAPI process AND the separate app.py Streamlit
# subprocess (see start()'s own docstring) - two different boot_ids
# by design, coordinated ONLY by this file lock. The heartbeat was
# ONLY ever refreshed by _tracking_log, i.e. only when the running job
# itself logged a line - the design assumed nightly_scan.py's own
# per-25-ticker progress lines (plus every per-ticker retry line)
# would always keep that well under _JOB_LOCK_FOREIGN_BOOT_STALE_
# SECONDS. The night this fired for real, Railway logs show: "[nightly
# _scan] reclaimed stale scan lock held by <boot_id> (different
# process), heartbeat 175s old" - the FIRST process's catch-up scan
# was still genuinely alive (mid-run, between universes - almost
# certainly a quiet stretch with no per-ticker line at all, e.g.
# resolving the next universe's own ticker list) when the SECOND
# process's own tick saw a 175s-old heartbeat, past the 120s foreign-
# boot threshold, and correctly-by-its-own-logic reclaimed a lock that
# was not actually abandoned - then started its OWN full catch-up scan
# of the same universe list, racing the first one on every yfinance
# call for the rest of the run (doubling load, worsening the same
# night's crumb 429s, and both writing scan_store saves for the same
# universes). The fix (see _record_job() below) stops coupling
# heartbeat freshness to the job's own logging cadence at all - a
# periodic refresh, tied only to wall-clock time, now runs regardless
# of whether the job has logged anything, which provably keeps the
# heartbeat under _JOB_LOCK_ACTIVE_REFRESH_SECONDS old for as long as
# the job's worker thread is alive, full stop - not "usually every 10-
# 25 tickers, until it isn't."
_JOB_LOCK_HEARTBEAT_STALE_SECONDS = 10 * 60
_JOB_LOCK_FOREIGN_BOOT_STALE_SECONDS = 2 * 60

# URGENT Commit 3 (25 Sep 2026, owner-reported): _record_job()'s own
# periodic heartbeat-refresh cadence - independent of job logging, see
# the comment above. Comfortably under _JOB_LOCK_FOREIGN_BOOT_STALE_
# SECONDS (120s) with a wide margin (4x), so a genuinely-alive job's
# lock can never again drift into "looks abandoned to a different
# process" territory purely because it went quiet for a while.
_JOB_LOCK_ACTIVE_REFRESH_SECONDS = 30

# Generated once when this module is first imported (i.e. once per
# process boot) - identifies THIS process's lifetime across every lock
# it acquires, so a lock can be told apart from "abandoned by some
# earlier boot" vs "still held by the boot that's asking right now".
_BOOT_ID = uuid.uuid4().hex


def _lock_path(job_name):
    base = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH") or os.path.dirname(__file__)
    return os.path.join(base, f"scheduler_{job_name}.lock")


def _new_lock_payload():
    return {
        "pid": os.getpid(),
        "boot_id": _BOOT_ID,
        "started": datetime.now(timezone.utc).isoformat(),
        "heartbeat": time.time(),
    }


def _read_lock_payload(path):
    """The lock file's parsed {"pid", "boot_id", "started", "heartbeat"}
    dict, or None if the file doesn't exist, can't be read, isn't valid
    JSON (the old plain-text "pid=... started=..." format fails here),
    or IS valid JSON but is missing "boot_id"/"heartbeat" (a legacy
    payload some future format change left behind). None is the
    "nothing usable to evaluate" signal _acquire_job_lock treats as
    immediately reclaimable."""
    try:
        with open(path) as f:
            raw = f.read()
    except OSError:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or "boot_id" not in data or "heartbeat" not in data:
        return None
    return data


def _acquire_job_lock(job_name, log):
    """True if this process just acquired the lock for `job_name` (caller
    must call _release_job_lock when done); False if another process
    already holds it (and its heartbeat is still fresh enough to trust).
    Fails OPEN (returns True without a real lock) on any filesystem
    error, matching this module's existing single-replica fail-safe
    stance - a lock that can't be checked should never be the reason the
    scheduler stops running altogether. `log` is used only to report a
    reclaim loudly (see the module comment above for the exact
    conditions) - every call site is already inside _loop(log)."""
    path = _lock_path(job_name)
    try:
        if os.path.exists(path):
            payload = _read_lock_payload(path)
            reclaim_msg = None
            if payload is None:
                reclaim_msg = (
                    f"[scheduler] reclaimed stale scan lock for '{job_name}' "
                    f"(legacy lock format, no heartbeat/holder fields)"
                )
            else:
                age = time.time() - float(payload["heartbeat"])
                holder_boot_id = payload.get("boot_id")
                if age > _JOB_LOCK_HEARTBEAT_STALE_SECONDS:
                    reclaim_msg = (
                        f"[scheduler] reclaimed stale scan lock held by "
                        f"{holder_boot_id}, heartbeat {age:.0f}s old"
                    )
                elif holder_boot_id != _BOOT_ID and age > _JOB_LOCK_FOREIGN_BOOT_STALE_SECONDS:
                    reclaim_msg = (
                        f"[scheduler] reclaimed stale scan lock held by "
                        f"{holder_boot_id} (different process), heartbeat {age:.0f}s old"
                    )
            if reclaim_msg:
                log(reclaim_msg)
                try:
                    os.remove(path)
                except OSError:
                    pass
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps(_new_lock_payload()))
        return True
    except FileExistsError:
        return False
    except OSError:
        return True


def _refresh_job_lock_heartbeat(job_name):
    """Rewrites job_name's lock file with a fresh heartbeat, IF this
    process still holds it (same boot_id) - called on every log line a
    running job emits (see _record_job's _tracking_log wrapper), which
    fires far more often than the staleness thresholds above, so a
    genuinely-alive job's own lock never goes stale out from under it.
    A no-op if the lock is missing/unreadable, or (should never happen
    in normal operation - would mean we were already reclaimed) held by
    a different boot_id: refreshing a lock this process doesn't
    currently hold would defeat the reclaim this fix exists to do."""
    path = _lock_path(job_name)
    payload = _read_lock_payload(path)
    if payload is None or payload.get("boot_id") != _BOOT_ID:
        return
    payload["heartbeat"] = time.time()
    try:
        with open(path, "w") as f:
            f.write(json.dumps(payload))
    except OSError:
        pass


def _release_job_lock(job_name):
    """Releases job_name's lock - but only if this process still holds
    it. If a long quiet stretch (no log lines) let another process's
    _acquire_job_lock decide our heartbeat was stale and reclaim it
    out from under us, the lock on disk now belongs to THEM; blindly
    os.remove()-ing it here would release a different process's
    legitimately-held lock instead of our own (already-gone) one. Only
    a payload we can positively read AND that names a different
    boot_id blocks the removal - anything else (genuinely ours, or
    unreadable/missing) falls through to the same unconditional remove
    this function always did."""
    path = _lock_path(job_name)
    payload = _read_lock_payload(path)
    if payload is not None and payload.get("boot_id") != _BOOT_ID:
        return
    try:
        os.remove(path)
    except OSError:
        pass


def _run_nightly(cfg, log, run_night=None):
    import nightly_scan
    import scanner_engine

    # Commit H (20 Sep 2026): captured ONCE, here, before any universe is
    # touched - this is what every universe scanned/repriced during this
    # call gets credited to (scan_store's own run_night field, and the
    # admin calendar's bump_scan_calendar day), regardless of how long
    # the run actually takes or what real wall-clock date it's IN
    # PROGRESS at wherever it happens to be right now. Root-caused
    # incident (14-20 Sep 2026): the regular due-scan block starts a run
    # at NIGHTLY_SCAN_UTC_HOUR (20:00 UTC default), and _universes_
    # needing_scan sorts smallest-first, so the largest universe due that
    # night - always the weekday-pinned one, by design - runs LAST. A
    # run that starts at 20:00 UTC and works through several smaller
    # daily universes before reaching a 500-600-ticker weekly-pinned one
    # can easily cross 00:00 UTC by the time that one finishes; the OLD
    # code stamped its admin-calendar marker (and the only "was this
    # scanned tonight" signal _universes_missing_today read) with
    # whatever the wall-clock date was AT THE MOMENT the marker was
    # written - the day AFTER the run started - so a scan that genuinely
    # ran on its scheduled night was recorded as missing, every single
    # week, for every weekday-pinned universe (see H2 below for the
    # other half of why it was always the LARGEST universes hitting
    # this).
    #
    # `run_night` is a parameter, not always self-computed, specifically
    # for the catch-up call site below in _loop(): a catch-up run for a
    # universe missing FROM LAST NIGHT starts executing tonight (or past
    # midnight), but must still be credited to the night it's catching
    # up, not the night it happens to run - so that call site passes its
    # own already-correct `ref_night` in explicitly. The regular due-scan
    # call site passes nothing, so this defaults to "now" at the moment
    # THIS run starts - which is exactly right for it, since a regular
    # run's own start time IS its scheduled night.
    run_night = run_night or datetime.now(timezone.utc).strftime("%Y-%m-%d")

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

    # Commit D (20 Sep 2026): re-price the market-cap ranking (the tail
    # fetch_asx300()/fetch_allords() slice from) BEFORE any universe is
    # scanned tonight, so both AU universes that depend on it see this
    # run's result rather than an earlier, in-process-cached fetch_
    # asx300()/fetch_allords() answer built from yesterday's ranking. A
    # failure here degrades to "tonight's AU scans use whatever ranking
    # was already on file" (nightly_scan.refresh_market_cap_ranking()
    # itself already fails open the same way scanner_engine's own fetch_
    # asx_listed_companies() does), never blocks the scan loop below.
    try:
        nightly_scan.refresh_market_cap_ranking(log=log)
    except Exception as e:
        log(f"[scheduler] market-cap ranking refresh failed: {e}")

    # Commit F (time-critical fix): fetch_asx300()/fetch_allords()/fetch_
    # asx_listed_companies() are st.cache_data(ttl=86400) - same process
    # as this scheduler, so if a web visitor called any of them earlier
    # today BEFORE tonight's refresh above (including catching a None
    # when no ranking existed on disk yet), that stale/None answer stays
    # cached for up to 24h regardless of the fresh file just written -
    # the scan loop below would call get_universe_pool(), hit the same
    # cached None, and tonight's ASX 300/All Ordinaries scans would
    # produce nothing. Clearing these three right after the refresh
    # forces the scan loop's own first call this run to read fresh.
    # _asx_non200_by_marketcap() itself carries no cache decorator (a
    # plain file read - see its own docstring) so it needs no clearing.
    try:
        scanner_engine.fetch_asx300.clear()
        scanner_engine.fetch_allords.clear()
        scanner_engine.fetch_asx_listed_companies.clear()
    except Exception as e:
        log(f"[scheduler] market-cap ranking cache clear failed: {e}")

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
    # Part 48.2(c): every ticker scanned tonight, across every universe
    # (real ones and "imported" alike) - the candidate pool the sector-
    # cache top-up draws from after the loop below. Collected here rather
    # than re-derived later since `payload["rows"]` (already in hand per
    # universe) is exactly the "was scanned tonight" fact the top-up
    # needs, and nothing else after this loop still has it this cheaply.
    _tickers_scanned_tonight = []
    # Discovery drop-and-reweight fix, Part 2 (18 Sep 2026): universes
    # scanned attention_lite=True tonight - the candidate pool the
    # attention top-up loop below draws from, same "collected here,
    # consumed after the loop" shape as _tickers_scanned_tonight above.
    _lite_universes_scanned_tonight = []
    for universe in ordered:
        try:
            if universe == nightly_scan.IMPORTED_UNIVERSE:
                payload = nightly_scan.run_imported_scan(log=log)
            else:
                payload = nightly_scan.run_universe_scan(universe, log=log, run_night=run_night)
            # AI-readiness Phase 1 (AI_ROADMAP_stocksdeepdive.md): build the
            # public /s/<TICKER> snapshot + /api/v1 data for this universe
            # right after its scan lands - re-shapes rows the scan just
            # computed, no extra network calls. A failure here must never
            # take down the scan it rides on.
            if payload and payload.get("rows"):
                _tickers_scanned_tonight.extend(
                    r.get("Ticker") for r in payload["rows"] if r.get("Ticker"))
                if payload.get("attention_lite"):
                    _lite_universes_scanned_tonight.append(universe)
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

    # Part 48.2(c): sector-cache top-up, once, over every ticker scanned
    # tonight above - see nightly_scan.run_sector_topup()'s own docstring
    # for the cap/pacing/failure-silent guarantees. Runs strictly after
    # the scan loop (never inside it) since it needs the FULL de-duplicated
    # list of tonight's tickers, not just one universe's, to pick its
    # batch fairly across whichever universes ran tonight.
    try:
        nightly_scan.run_sector_topup(_tickers_scanned_tonight, log=log)
    except Exception as e:
        log(f"[scheduler] sector top-up failed: {e}")

    # Discovery drop-and-reweight fix, Part 2 (18 Sep 2026): attention
    # top-up, once per universe that was scanned attention_lite=True
    # tonight above (real universes AND catch-up universes alike - this
    # whole function is what the catch-up block calls too, via the SAME
    # _acquire_job_lock("nightly")-protected path, so nothing extra is
    # needed to cover that case). See nightly_scan.run_attention_topup()'s
    # own docstring for the cap/pacing/failure-silent guarantees - same
    # shape as the sector top-up immediately above. Deliberately placed
    # here, strictly BEFORE _build_derived_universes() further down,
    # which re-reads each parent's scan_store rows fresh off disk - so a
    # derived universe (ASX 100/Small Ords/Russell 3000/etc.) and the
    # home page's "Tonight's top 5" both see the topped-up Long Scores,
    # not the pre-topup ones.
    for _universe in _lite_universes_scanned_tonight:
        try:
            nightly_scan.run_attention_topup(_universe, log=log)
        except Exception as e:
            log(f"[scheduler] attention top-up {_universe} failed: {e}")

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

    # Part 34 addendum 34.7 (11 Sep 2026): nightly reprice pass - every
    # REAL universe (one with its own cadence entry in NIGHTLY_UNIVERSES,
    # i.e. cfg["universe_cadence"] - derived universes have no cadence
    # entry at all and are rebuilt below instead, never repriced
    # directly) that did NOT get a full scan tonight gets its stored
    # rows' price/MOS/psychology/discovery/value score refreshed via a
    # batch download - see nightly_scan.reprice_universe()'s own
    # docstring. `ordered` (built above) is tonight's actual full-scan
    # list, "imported" excluded since it isn't a scanner_engine universe
    # at all. Runs inside the SAME "nightly" job lock this whole function
    # already executes under (see _loop()'s _acquire_job_lock("nightly")
    # call) and strictly after every full scan above - satisfying the
    # addendum's "same single-process lock... same scheduler slot, after
    # the scans" guard without any extra locking code needed here.
    try:
        scanned_tonight = {u for u in ordered if u != nightly_scan.IMPORTED_UNIVERSE}
        to_reprice = [u for u in cfg.get("universe_cadence", {}).keys() if u not in scanned_tonight]
        if to_reprice:
            log(f"[scheduler] reprice pass: {len(to_reprice)} universe(s) not scanned "
                f"tonight ({', '.join(to_reprice)})")
            for universe in to_reprice:
                try:
                    nightly_scan.reprice_universe(universe, log=log, run_night=run_night)
                except Exception as e:
                    log(f"[scheduler] reprice {universe} failed: {e}")
        else:
            log("[scheduler] reprice pass: every real universe was scanned tonight, nothing to reprice")
    except Exception as e:
        log(f"[scheduler] reprice pass failed: {e}")

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
    # Part 34.1/34.2 (11 Sep 2026): AU + US sector universes - same
    # membership-filter mechanism, parent(s) unchanged from what each
    # sector filters ("ASX 300" for AU, "S&P 500" for US - see
    # scanner_engine._ASX_SECTOR_UNIVERSE_MAP/_US_SECTOR_UNIVERSE_MAP).
    "ASX Financials": ["ASX 300", "ASX 200"],
    "ASX Materials & Mining": ["ASX 300", "ASX 200"],
    "ASX Health Care": ["ASX 300", "ASX 200"],
    "ASX Consumer": ["ASX 300", "ASX 200"],
    "ASX Industrials": ["ASX 300", "ASX 200"],
    "ASX A-REITs": ["ASX 300", "ASX 200"],
    "US Technology": ["S&P 500"],
    "US Healthcare": ["S&P 500"],
    "US Financials": ["S&P 500"],
    "US Energy": ["S&P 500"],
    "US Industrials": ["S&P 500"],
    "US Consumer": ["S&P 500"],
    # Part 34.3 (11 Sep 2026): Russell 3000, derived union of the two
    # already-scanned Russell parents - same pattern as S&P 1500 above.
    "Russell 3000": ["Russell 1000", "Russell 2000"],
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

            # Commit 1 (23 Sep 2026, owner-reported): same integrity
            # guard nightly_scan.run_universe_scan() applies to the
            # independently-scanned chain members - see scanner_
            # engine.verify_universe_before_save()'s own docstring.
            # Only the tracked universes among this function's own
            # derived set (Small Ordinaries/100/50/20 - sector
            # universes and Russell 3000/S&P 1500 aren't chain
            # members, so this is a no-op for them). A failing
            # universe is NOT saved - whatever was already on disk
            # stays put, same as the "no membership resolved"/"none
            # of its members have a scanned parent row yet" skips
            # just above.
            _derived_degraded = False
            if universe in scanner_engine.UNIVERSE_INTEGRITY_TRACKED_UNIVERSES:
                import source_health_store
                import alert_engine
                _integrity_source = f"Universe integrity: {universe}"
                _integrity_ok, _integrity_reason = scanner_engine.verify_universe_before_save(
                    universe, [r.get("Ticker") for r in rows], log=log)
                if not _integrity_ok:
                    _integrity_prior = source_health_store.get(_integrity_source)
                    _integrity_was_stale = bool(_integrity_prior and _integrity_prior.get("stale"))
                    source_health_store.record_failure(
                        _integrity_source,
                        {"containment": {"ok": False, "detail": _integrity_reason}},
                        _integrity_reason,
                    )
                    if not _integrity_was_stale:
                        try:
                            alert_engine.send_source_health_alert(
                                _integrity_source,
                                {"containment": {"ok": False, "detail": _integrity_reason}},
                                _integrity_reason,
                            )
                        except Exception as e:
                            log(f"[scheduler] derived universe integrity alert failed for {universe}: {e}")
                    # URGENT (24 Sep 2026, owner-reported): same fix as
                    # nightly_scan.run_universe_scan()'s own matching
                    # guard - never skip a save that has nothing
                    # servable to fall back to. scan_store.load_scan()
                    # is the exact "would the Scanner page actually
                    # serve something right now" test (None on no file
                    # or past its 72h freshness cutoff).
                    _integrity_prior_scan = scan_store.load_scan(universe)
                    if _integrity_prior_scan is None:
                        log(f"[scheduler] derived universe {universe}: integrity guard FAILED - "
                            f"{_integrity_reason} - but nothing servable is on disk - saving "
                            f"anyway, flagged degraded, rather than leaving the page blank.")
                        _derived_degraded = True
                    else:
                        log(f"[scheduler] derived universe {universe}: integrity guard FAILED - "
                            f"{_integrity_reason} - NOT saving; keeping last known good.")
                        continue
                else:
                    source_health_store.record_success(
                        _integrity_source, [],
                        {"containment": {"ok": True, "detail": _integrity_reason}},
                    )

            scan_store.save_scan(universe, rows, f"Derived from {'/'.join(parents)} ({source})",
                                  degraded=_derived_degraded)
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
    why. Import deferred (a background job's heavy imports shouldn't slow
    every other scheduler tick).

    18 Sep 2026 retry fix: this used to catch-and-log its own failure
    here, so it never raised and the scheduler thread was never at risk -
    but that also meant the watchdog guard in _loop() below had no way to
    tell a failed run from a successful one, and marked the day done
    either way. Now lets the exception propagate instead, so _loop()'s
    guard can persist "done for today" only on real success and retry
    (within its attempt cap) otherwise. The scheduler thread is still
    never at risk from this: _loop()'s own per-tick try/except (and, one
    layer in, the guard's own try/except around this call - see that
    comment) both catch it; only the log text/call site moved."""
    import portfolio_watchdog_engine
    portfolio_watchdog_engine.run_nightly_watchdog(log=log)


def _run_quote_recorder_asx(log):
    """Trading Cost tab, Commit 1: the ASX-local mid-session quote
    sampler - see quote_recorder.py's own module docstring for what it
    records, why (Yahoo's after-hours bid/ask snapshot is junk), and
    the four rejection rules. Deferred import, same shape as every
    other _run_* job above. Runs regardless of ENABLE_TRADING_COST -
    this only writes to quote_snapshot_store's own table, nothing a
    visitor sees changes.

    Lets the exception propagate (same "let it raise" contract as
    _run_watchdog/_run_backup above) so the _loop() guard's retry-cap
    only marks the day done on a genuine success - a bad ticker inside
    the run is already handled without raising (quote_recorder.py's own
    per-ticker try/except), so an exception escaping this far means the
    run as a whole broke, not just one ticker."""
    import quote_recorder
    quote_recorder.run_asx_recorder(log=log)


def _run_quote_recorder_us(log):
    """US-local mid-session quote sampler - see _run_quote_recorder_asx
    above, same contract, same module."""
    import quote_recorder
    quote_recorder.run_us_recorder(log=log)


def _run_top100(log):
    """Top 100 tab, Commit 1: the nightly Top 100 selection/scoring
    pass - see top100_engine.run_nightly()'s own docstring for the
    three stages (re-select the pool, poll any in-flight AI-scoring
    Batch API submission, then submit a new one for whatever's still
    unscored). Import deferred, same shape as every other _run_* job
    above. Lets the exception propagate (same "let it raise" contract
    as _run_backup/_run_watchdog above) so the _loop() guard's retry-
    cap only marks the day done on a genuine success - top100_engine's
    own per-stage guarding (a bad universe file skipped, a bad batch
    result skipped, a submission failure leaving prior scores intact)
    already means an exception escaping this far is a real problem
    with the run as a whole, not a single ticker."""
    import top100_engine
    top100_engine.run_nightly(log=log)


def _run_top100_poll(log):
    """Top 100 Commit 2 (25 Sep 2026, owner-reported): poll-ONLY entry
    point for the hourly check in _loop() below - calls top100_engine.
    poll_and_ingest_batch() directly and NOTHING else: never select_
    top100_pool(), never submit_nightly_batch(). Selection/submission
    stay exclusively the nightly "top100" job's job (_run_top100 above,
    still the only caller of run_nightly()).

    Why this exists: poll_and_ingest_batch() otherwise only ever ran
    inside that once-a-day nightly job, so a batch that finished (or
    errored) within the hour Anthropic itself expects ("most complete
    within 1 hour" per the Batches API) sat un-ingested for up to 24h
    - and while it sat there, top100_store.get_batch_state() being
    non-None also blocked the NEXT night's submit_nightly_batch() from
    firing at all (only one batch in flight at a time). This hourly
    poll ingests as soon as a batch actually finishes, so: a
    successful batch's scores reach the page within about an hour
    instead of up to a day, and a FAILED batch (e.g. one submitted
    under an old, since-fixed request shape) gets its batch-state row
    cleared within about an hour too, so the very next nightly job's
    own submit_nightly_batch() call goes out fresh rather than waiting
    behind a stale failure it hadn't even looked at yet.

    Let the exception propagate (same "let it raise" contract as
    _run_top100 above) - poll_and_ingest_batch() already guards its own
    per-result failures internally (a bad result is logged and
    skipped, prior scores are never touched), so an exception escaping
    THIS far means something is wrong with the poll as a whole (e.g.
    the Anthropic client itself failing to construct), not one result."""
    import top100_engine
    top100_engine.poll_and_ingest_batch(log=log)


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


def _run_backup(log):
    """Mega-batch Part 10: the nightly off-site DB backup. Import
    deferred, same shape as _run_digest/_run_watchdog above.

    18 Sep 2026 retry fix: used to catch-and-log its own failure here
    (see db_backup_engine's own once-per-failure-streak emailing rule for
    the owner-visible side of that), which meant a failed or killed
    backup never raised and the _loop() guard below had no way to tell,
    so it marked the day "done" and never retried until tomorrow night -
    exactly the failure mode an off-site backup can least afford. Now
    lets the exception propagate so that guard can persist "done for
    today" only once this genuinely returns without raising, and retry
    (within its attempt cap) otherwise - see that guard's own comment.
    Nothing about failure VISIBILITY changes: _record_job() still logs
    and records the failure to admin_metrics_store before it propagates,
    and the guard's own except still logs the same
    "[scheduler] db backup failed: ..." line this function used to log
    itself."""
    import db_backup_engine
    db_backup_engine.run_nightly_backup(log=log)


def _run_volume_check(log):
    """Mega-batch Part 16: the nightly Volume usage check + retention
    prune. Same shape as _run_backup above - import deferred,
    volume_monitor.run_nightly_check() is itself already fully guarded
    internally, this is one more outer safety net."""
    try:
        import volume_monitor
        volume_monitor.run_nightly_check(log=log)
    except Exception as e:
        log(f"[scheduler] volume monitor failed: {e}")
    # Part 35.1: prune the signed-in-account hash table on the same
    # nightly slot as every other retention prune above - see
    # admin_metrics_store.prune_old_signin_hashes()'s own docstring for
    # why this table needs pruning at all (privacy: no hash should
    # outlive the window any admin tile reads) and why it's safe here
    # (this whole function already only ever logs on failure, never
    # raises to its caller).
    if admin_metrics_store is not None:
        try:
            admin_metrics_store.prune_old_signin_hashes(log=log)
        except Exception as e:
            log(f"[scheduler] signin-hash prune failed: {e}")
        try:
            admin_metrics_store.prune_old_counters(log=log)
        except Exception as e:
            log(f"[scheduler] pulse-counter prune failed: {e}")
        # Part 53.3: same nightly slot, same retention window (90 days)
        # as the two prunes above - see admin_metrics_store's module
        # docstring ("PART 53.3 EXCEPTION...") for why this table exists
        # and needs pruning at all.
        try:
            admin_metrics_store.prune_old_account_signins(log=log)
        except Exception as e:
            log(f"[scheduler] account-signin prune failed: {e}")
    # Mega-batch Part 36: same nightly slot, same "log on failure only,
    # never raise" contract - see newsletter_store.prune_stale_
    # unconfirmed's own docstring for exactly what it deletes.
    if newsletter_store is not None:
        try:
            newsletter_store.prune_stale_unconfirmed(log=log)
        except Exception as e:
            log(f"[scheduler] newsletter prune failed: {e}")
    # Trading Cost tab, Commit 1: same nightly slot, same contract - see
    # quote_snapshot_store.prune_old()'s own docstring (400-day
    # retention on both its tables). Deferred import, same "don't slow
    # every scheduler tick" reasoning as every job above.
    try:
        import quote_snapshot_store
        quote_snapshot_store.prune_old(log=log)
    except Exception as e:
        log(f"[scheduler] quote snapshot prune failed: {e}")


# 17 Sep 2026 (restart-resilience fix): how long after the scan hour a
# catch-up scan is still allowed to fire for that night - see
# _catchup_reference_night() below for the exact window check. Kept
# short and deliberate: long enough to cover a redeploy that kills a
# scan mid-run and the container restarting soon after (the incident
# this fix responds to: a 21:46 UTC deploy killed a 20:01-started scan;
# a same-day restart is the case this exists for), short enough that an
# unrelated restart hours later - the afternoon of the same UTC day, or
# any day after - never fires a scan at a time nobody would expect one.
# Past this window, a universe that's still missing its scan for the
# night simply waits for the regular due-scan check
# (_universes_needing_scan) to pick it up once its own staleness
# threshold trips, exactly as it always has.
CATCHUP_WINDOW_HOURS = 6


def _catchup_reference_night(cfg, now):
    """Returns the UTC calendar date (a 'YYYY-MM-DD' string) of the scan
    night `now` is a valid catch-up moment for, or None if `now` isn't
    within CATCHUP_WINDOW_HOURS of any scan-hour instant.

    Checks BOTH today's and yesterday's scan-hour instant, not just
    `now.hour >= cfg["scan_hour"]` - the window can cross UTC midnight
    whenever scan_hour + CATCHUP_WINDOW_HOURS > 24 (the default
    scan_hour=20 + a 6h window reaches 02:00 the next day). A naive
    same-calendar-day check would incorrectly fall out of the window
    right at midnight even though the scan night being caught up on
    hasn't changed - checking yesterday's instant too means a catch-up
    firing at, say, 01:00 UTC correctly resolves to YESTERDAY's scan
    night (the one it's actually catching up), not today's (which
    hasn't reached its own scan hour yet)."""
    scan_hour = cfg["scan_hour"]
    for days_back in (0, 1):
        ref_date = (now - timedelta(days=days_back)).date()
        scan_instant = datetime(
            ref_date.year, ref_date.month, ref_date.day,
            scan_hour, 0, 0, tzinfo=timezone.utc)
        if scan_instant <= now < scan_instant + timedelta(hours=CATCHUP_WINDOW_HOURS):
            return ref_date.strftime("%Y-%m-%d")
    return None


def nightly_scan_window_active(now=None):
    """Commit 6 (24 Sep 2026, owner-reported): True if `now` (UTC,
    defaults to the current time) falls within the nightly scan's own
    operating window - reuses _catchup_reference_night()'s exact
    scan_hour..scan_hour+CATCHUP_WINDOW_HOURS logic (including its own
    UTC-midnight-crossing handling), the same "is this a valid moment
    for the nightly scan to be running" test the scheduler's own catch-
    up path already relies on - rather than a second, naive fixed-hour
    check living in a second place. Also true if the "nightly" job lock
    is held with a fresh heartbeat, in case an unusually long-running
    scan is still going slightly outside that window.

    Read-only: never acquires, refreshes, or releases the lock, and
    never blocks. Added for the Admin Dashboard's consolidated Moat-
    preview export (moat_export.py), which must refuse to run - and
    stop adding load that competes for the same cached-bundle reads -
    during this window, rather than hand-roll a fixed-hour check of its
    own."""
    now = now or datetime.now(timezone.utc)
    if _catchup_reference_night(_cfg(), now) is not None:
        return True
    payload = _read_lock_payload(_lock_path("nightly"))
    if payload is None:
        return False
    try:
        age = time.time() - float(payload.get("heartbeat", 0))
    except (TypeError, ValueError):
        return False
    return age <= _JOB_LOCK_HEARTBEAT_STALE_SECONDS


def _universes_missing_today(cfg, ref_day):
    """Universes that were SCHEDULED for the `ref_day` ('YYYY-MM-DD' UTC
    date - see _catchup_reference_night above) scan night but have no
    scan_store entry credited to that night - i.e. never got a full scan
    saved for it, whether because a restart killed the run before
    reaching them or (17 Sep 2026 fix, see the due-scan block in _loop()
    below) a lock-contention tick burned an attempt without ever
    starting one.

    "Scheduled" mirrors _render_scan_calendar_html's (app.py) own admin-
    calendar convention exactly: "daily" is scheduled every night; a
    weekday-pinned cadence (mon..sun) is scheduled only on ITS OWN
    weekday; a bare "weekly" (no pinned day) is never flagged missing
    here, same as the admin calendar's own "Scheduled" column - there's
    no single day to check a bare-weekly universe against, so it's left
    entirely to the general due-scan check (_universes_needing_scan)
    to pick up on whichever night its own staleness clock fires, exactly
    as before this feature existed.

    Deliberate interpretation note (documented per the task instruction
    to flag this): the admin calendar's own "Scheduled"/"missing" grid
    (admin_metrics_store.bump_scan_calendar / scan_calendar_grid) is
    actually driven by a separate pulse-counter event log, not by
    scan_store directly. This function reads scan_store.load_scan_raw()
    instead - the more authoritative underlying source for "was a real
    scan for this universe saved for this night" - while matching the
    calendar's SCHEDULING convention (daily / pinned-weekday / bare-
    weekly) exactly. Uses load_scan_raw() rather than load_scan() so a
    scan credited to ref_day still counts even were it to have since
    aged past load_scan()'s 72h display cutoff - not realistic given
    this only ever runs within a few hours of ref_day, but it's the
    correct/authoritative accessor regardless (see that function's own
    docstring in scan_store.py).

    Commit H (20 Sep 2026): checks the payload's own `run_night` field
    first - the scheduled night scheduler_engine._run_nightly() captured
    at the START of the run that saved it (see save_scan()'s own
    docstring) - falling back to generated_at's date only for an older
    payload saved before this field existed, or one saved with no
    scheduler context at all (a hand-run scan). Checking generated_at
    ALONE (the pre-Commit-H behavior) was the root cause of a real
    incident: a weekday-pinned universe is always the LARGEST one due
    that night (see _scan_priority_key's own comment) and used to be
    scanned dead last, so a run starting at NIGHTLY_SCAN_UTC_HOUR could
    easily still be working through it after the UTC date rolled over -
    generated_at (real wall-clock completion time) then landed on the
    FOLLOWING day, so a scan that genuinely ran on its scheduled night
    was recorded as missing here every single week, for every weekday-
    pinned universe, with no generated_at-based check ever able to tell
    a late-finishing real scan apart from one that never happened.

    Sorted by _scan_priority_key (Commit H: pinned universes first, then
    smallest-first within each group - see that function's own
    docstring), so a catch-up run that's interrupted again still gets to
    the universe that can least afford another missed week."""
    import scan_store
    ref_date = datetime.strptime(ref_day, "%Y-%m-%d").date()
    ref_weekday = ref_date.weekday()
    missing = []
    for u, cadence in cfg["universe_cadence"].items():
        if cadence in _WEEKDAY_ABBR:
            if _WEEKDAY_ABBR[cadence] != ref_weekday:
                continue  # not this universe's scheduled night
        elif cadence == "weekly":
            continue  # no pinned day - left to the general due-scan check
        # cadence == "daily" falls through: scheduled every night
        payload = scan_store.load_scan_raw(u)
        credited_day = None
        if payload:
            credited_day = payload.get("run_night")
            if not credited_day:
                try:
                    credited_day = datetime.fromisoformat(payload["generated_at"]).date().strftime("%Y-%m-%d")
                except (KeyError, ValueError):
                    credited_day = None
        if credited_day != ref_day:
            missing.append(u)
    missing.sort(key=lambda u: _scan_priority_key(u, cfg["universe_cadence"]))
    return missing


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

    Commit H (20 Sep 2026): result is sorted by _scan_priority_key - a
    weekday-pinned universe first, then dailies, smallest-first within
    each group (see that function's own docstring for why this order,
    not the reverse: a daily universe missing tonight is due again in
    ~20h; a weekday-pinned one gets exactly one chance a week, so if a
    night's run is cut short - or simply doesn't finish before the UTC
    date rolls over - it's the pinned universe, not a daily one, that
    can't afford to be the one left out)."""
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
    due.sort(key=lambda u: _scan_priority_key(u, cfg["universe_cadence"]))
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

# Top 100 Commit 2 follow-up (25 Sep 2026, owner-reported): True once
# THIS process's own _loop() has made (or is making) its one boot-time
# immediate attempt at the hourly Top 100 poll - see that block's own
# comment for why a fresh boot needs an immediate shot at a pending
# batch regardless of the persisted inter-poll interval. Same "plain
# module global, not persisted" reasoning as _last_heartbeat above -
# this is specifically about THIS process's own boot, so there is
# nothing to persist across a restart; a NEW process gets its own
# fresh False and thus its own fresh immediate attempt.
_top100_boot_poll_attempted = False

# CRITICAL (25 Sep 2026, owner-reported): wall-clock time of the last
# "[scheduler] alive" heartbeat LOG line (distinct from _last_heartbeat
# above, which is an in-process timestamp _record_job/heartbeat_age_
# seconds() reads, but is never itself visible in the logs unless some
# job happens to log something that tick). A container can boot, tick
# silently for hours doing nothing wrong (nothing due yet that hour),
# and be indistinguishable in the logs from a container whose loop
# thread died on tick 1 - this week's three outages were each invisible
# for hours for exactly that reason. Plain module global, same
# process-local reasoning as _last_heartbeat/_top100_boot_poll_attempted
# above - nothing to persist across a restart.
_last_heartbeat_log_at = None


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


# URGENT Commit 3 (24 Sep 2026, owner-reported): per-job hard timeout
# ceiling, keyed by job_name (see _record_job()'s own comment) - the
# fix for the earnings-calendar refresh hanging on a network call with
# no timeout at all and wedging this single scheduler thread for ~3h,
# blocking every other job behind it (scan, digest, watchdog, backup,
# top100) until a redeploy freed it. Generous enough that no legitimate
# run should ever hit it - "nightly" in particular can genuinely run
# over an hour on a big universe, now with retry backoff on top (see
# nightly_scan.py's own crumb-retry fix) - but bounded, so a genuinely
# wedged call can never again block indefinitely. "earnings_refresh"
# is deliberately tight: EARNINGS_WEEKLY_CAP already caps it to a small
# per-run batch, so a run anywhere near this ceiling IS the hang, not
# normal variance.
_JOB_HARD_TIMEOUT_SECONDS = {
    "nightly": 4 * 3600,
    "watchdog": 10 * 60,
    "quote_recorder_asx": 15 * 60,
    "quote_recorder_us": 15 * 60,
    "backup": 30 * 60,
    "volume_check": 5 * 60,
    "top100": 10 * 60,
    # Top 100 Commit 2 (25 Sep 2026, owner-reported): the hourly poll-
    # only job (see _run_top100_poll and its own _loop() block) - a
    # single batch retrieve() plus, on "ended", iterating and saving
    # up to MAX_NIGHTLY_SCORES results. Never selects or submits (that
    # stays nightly-only, under the "top100" ceiling above), so this
    # ceiling is deliberately much tighter than "top100"'s own 10min.
    "top100_poll": 5 * 60,
    "earnings_refresh": 15 * 60,
    "digest": 15 * 60,
}
_JOB_HARD_TIMEOUT_DEFAULT_SECONDS = 15 * 60

# URGENT (25 Sep 2026, owner-reported): minimum spacing between hourly
# Top 100 poll ATTEMPTS (see _loop()'s own comment on its dispatch
# block) - deliberately named "attempt", not "success": the timestamp
# is persisted before _record_job() runs, so a failed/timed-out
# attempt still advances it and the interval still holds, rather than
# retrying every single tick on a persistent failure.
_TOP100_POLL_MIN_INTERVAL_SECONDS = 60 * 60

# CRITICAL (25 Sep 2026, owner-reported): how often _loop() logs an
# unconditional "[scheduler] alive" heartbeat line, regardless of
# whether any job was due that tick - see _last_heartbeat_log_at's own
# docstring for why silence alone is not distinguishable from a dead
# loop without this.
_HEARTBEAT_LOG_INTERVAL_SECONDS = 30 * 60


def _record_job(job_name, log, run_fn, lock_name=None):
    """Times run_fn(wrapped_log) and records the result to
    admin_metrics_store's job_status table for the Admin Dashboard's
    NIGHTLY JOBS table - WITHOUT changing any _run_* function's own
    body. Every _run_* job already wraps its own work in try/except and
    logs a line containing "failed" rather than raising (so one bad
    universe/ticker never stops the rest of that job) - wrapping the log
    function they're already given, rather than their return value
    (there isn't one), lets this count how many "failed" lines a run
    logged with zero changes to any job's existing, already-tested error
    handling:
      - 0 "failed" lines logged  -> 'ok'
      - >=1 "failed" line logged -> 'warn' (the job completed, but at
        least one step inside it didn't)
      - an exception escapes run_fn entirely (every _run_* function's
        own top-level try/except means this should be rare - one more
        outer safety net, same convention as _loop()'s own try/except
        around the whole tick) -> 'error', then re-raised so the
        existing job-lock `finally` and outer loop error handling still
        see it exactly as before this existed.
    Never changes whether/how the job itself runs; the metrics write
    itself is try/except-guarded so a logging bug here can never take a
    real job down.

    18 Sep 2026 heartbeat fix: this same wrapper is also the natural
    place to refresh the job lock's heartbeat (see
    _refresh_job_lock_heartbeat's own docstring). `lock_name` (URGENT
    Commit 3, 25 Sep 2026) is the string used at the matching
    _acquire_job_lock/_release_job_lock call site - defaults to
    job_name, true for every call site except the hourly Top 100 poll
    (see _loop()'s own comment there for why that one legitimately
    differs: it shares the nightly job's "top100" lock but reports
    under its own "top100_poll" name for admin-metrics purposes).

    URGENT Commit 3 (25 Sep 2026, owner-reported): heartbeat refresh
    used to happen ONLY inside _tracking_log, i.e. only when the
    running job itself logged a line - the original design assumed
    that would always be frequent enough. It wasn't: Railway logs from
    the night this fired for real show a genuinely-alive catch-up scan
    (mid-run, between universes) losing its lock to a SECOND process
    (server.py's and app.py's own independent scheduler threads - see
    start()'s own docstring for why both legitimately exist) after its
    heartbeat sat unrefreshed for 175s - past _JOB_LOCK_FOREIGN_BOOT_
    STALE_SECONDS (120s), simply because nothing it did happened to log
    a line for that stretch (most likely resolving the next universe's
    own ticker list). The second process then ran its own full scan of
    the same universes concurrently with the first, doubling yfinance
    load for the rest of the run. Fix: heartbeat refresh is now ALSO
    driven by wall-clock time alone, via the join-loop below - a job
    that logs constantly and a job that logs nothing for minutes both
    get their heartbeat refreshed at least every _JOB_LOCK_ACTIVE_
    REFRESH_SECONDS, provably keeping it well under the reclaim
    thresholds for as long as its worker thread is actually alive. See
    module-level comment above _JOB_LOCK_HEARTBEAT_STALE_SECONDS for
    the full incident writeup.

    run_fn executes on a SEPARATE worker thread (unrelated to the
    heartbeat fix above - this dates to the same-day EARLIER hard-
    timeout fix), and THIS thread (the scheduler's own single
    processing thread) joins it with a hard per-job-type ceiling
    (_JOB_HARD_TIMEOUT_SECONDS) instead of calling run_fn directly -
    the fix for the earnings-calendar refresh hanging on a network call
    with no timeout of its own and wedging this thread for ~3h,
    blocking every other job behind it until a redeploy freed it. On a
    timeout: logs loudly, records result "timeout" (a new bucket
    alongside ok/warn/error) to admin_metrics_store, and raises
    TimeoutError - same "log then re-raise, let the existing job-lock
    finally and outer loop error handling see it" contract every other
    failure already gets below, so nothing downstream needs to know
    timeouts are a new case. The worker thread itself is left running,
    as a daemon - Python has no safe way to force-kill a thread stuck in
    a blocking C-level network call - but it can never again hold up
    THIS thread past the ceiling, which is the actual guarantee this
    exists to make; a daemon thread is killed outright when the process
    exits, so nothing lingers past a redeploy either."""
    lock_name = lock_name or job_name
    fail_count = [0]

    def _tracking_log(msg):
        if "failed" in str(msg):
            fail_count[0] += 1
        _refresh_job_lock_heartbeat(lock_name)
        log(msg)

    timeout_seconds = _JOB_HARD_TIMEOUT_SECONDS.get(job_name, _JOB_HARD_TIMEOUT_DEFAULT_SECONDS)
    run_exc = [None]

    def _target():
        try:
            run_fn(_tracking_log)
        except BaseException as e:
            run_exc[0] = e

    t0 = time.time()
    result, detail = "ok", ""
    try:
        worker = threading.Thread(target=_target, name=f"scheduler-job-{job_name}", daemon=True)
        worker.start()
        # URGENT Commit 3: short joins in a loop instead of one big
        # worker.join(timeout_seconds) - refreshes lock_name's heartbeat
        # on every iteration REGARDLESS of whether run_fn has logged
        # anything, so a genuinely-alive job's lock can never again go
        # stale purely from a quiet stretch (see this function's own
        # docstring for the incident this closes). Same total wait
        # bound (timeout_seconds) and same is_alive()-after check as
        # before - only how the wait is broken up changed.
        while True:
            elapsed = time.time() - t0
            remaining = timeout_seconds - elapsed
            if remaining <= 0:
                break
            worker.join(min(_JOB_LOCK_ACTIVE_REFRESH_SECONDS, remaining))
            if not worker.is_alive():
                break
            _refresh_job_lock_heartbeat(lock_name)
        if worker.is_alive():
            elapsed = time.time() - t0
            result = "timeout"
            detail = f"exceeded {timeout_seconds}s hard timeout - abandoned, still running in background"
            log(f"[scheduler] {job_name}: TIMED OUT after {elapsed:.0f}s (hard limit "
                f"{timeout_seconds}s) - logging and moving on, not waiting any further; "
                f"the stuck call is abandoned, never force-killed")
            raise TimeoutError(f"{job_name} exceeded its {timeout_seconds}s hard timeout")
        if run_exc[0] is not None:
            raise run_exc[0]
        if fail_count[0]:
            result = "warn"
            detail = f"{fail_count[0]} step(s) logged a failure - see server logs"
    except BaseException as e:
        # CRITICAL (25 Sep 2026, owner-reported): BaseException, not
        # Exception - _target() above deliberately catches BaseException
        # from run_fn (line ~1637: "except BaseException as e: run_exc[0]
        # = e") and this block re-raises it via `raise run_exc[0]`
        # (line ~1671). Before this fix, THIS except was narrower
        # (Exception only) than what it re-raises, so a run_fn that
        # raised something outside Exception (SystemExit/
        # KeyboardInterrupt/GeneratorExit, or asyncio.CancelledError,
        # which stopped inheriting from Exception in Python 3.8) would
        # both (a) skip the `result, detail = "error", ...` assignment
        # just below, so admin_metrics_store's job_status row would
        # have logged this job as "ok" - the initial default - despite
        # it having actually failed, and (b) propagate straight past
        # this function's own try/except entirely uncaught, up to
        # whichever _loop() dispatch block called it (also only
        # `except Exception` at every one of those call sites) and from
        # there to the tick-level try/except - which, before this same
        # commit's fix there, was ALSO `except Exception`, meaning nothing
        # in the whole chain would have stopped it from killing the
        # scheduler thread outright. This function's own docstring already
        # calls _loop()'s tick-level try/except "one more outer safety
        # net" for exactly this case - it needs to actually be one.
        if result != "timeout":  # don't clobber the more specific bucket set above
            result, detail = "error", str(e)[:200]
        raise
    finally:
        if admin_metrics_store is not None:
            try:
                admin_metrics_store.record_job_result(
                    job_name, result, detail, time.time() - t0)
            except Exception:
                pass


def _process_role():
    """Returns "streamlit" (this process is app.py's own Streamlit
    subprocess) or "fastapi" (server.py's process) - purely for the
    boot/heartbeat
    log lines below, so a Railway log line can be told apart between
    the two processes that legitimately each run their own _loop()
    thread (see start()'s own docstring). Only app.py ever imports
    streamlit, so its presence in sys.modules is a reliable, zero-
    config way to tell the two apart without server.py/app.py having
    to pass an explicit role flag down to this module."""
    return "streamlit" if "streamlit" in sys.modules else "fastapi"


def _top100_poll_status_for_heartbeat():
    """Cheap, best-effort summary of the hourly Top 100 poll's own
    dispatch condition (see _loop()'s own comment on that block, and
    _TOP100_POLL_MIN_INTERVAL_SECONDS/_top100_boot_poll_attempted's
    docstrings) for the heartbeat log line - deliberately never lets an
    exception escape (a heartbeat must never itself become a new way
    for the loop to die), since it runs every single tick, not just
    the hourly ones the real dispatch block already guards this way."""
    try:
        if top100_store is None:
            return "n/a (top100_store unavailable)"
        if top100_store.get_batch_state() is None:
            return "n/a (nothing pending)"
        if not _top100_boot_poll_attempted:
            return "due now (boot attempt not yet made)"
        last_attempt = _load_state().get("last_top100_poll_attempt_at")
        if last_attempt is None:
            return "due now"
        remaining = _TOP100_POLL_MIN_INTERVAL_SECONDS - (time.time() - last_attempt)
        return "due now" if remaining <= 0 else f"in {int(remaining)}s"
    except Exception as e:
        return f"unknown ({e})"


def _loop(log):
    global _last_heartbeat, _top100_boot_poll_attempted, _last_heartbeat_log_at
    # CRITICAL (25 Sep 2026, owner-reported): unconditional, first line
    # of the function, before any work at all - a container boot with
    # NO line like this in its logs means this thread never even
    # reached here (start() itself raised, or was never called - see
    # start_with_retry()'s own docstring for the call-site half of this
    # fix), as distinct from a thread that reached here and is simply
    # idle because nothing happens to be due yet.
    log(f"[scheduler] loop started (pid={os.getpid()}, role={_process_role()})")
    while True:
        _last_heartbeat = time.time()
        if (_last_heartbeat_log_at is None
                or (_last_heartbeat - _last_heartbeat_log_at) >= _HEARTBEAT_LOG_INTERVAL_SECONDS):
            _last_heartbeat_log_at = _last_heartbeat
            log(f"[scheduler] alive, next due: top100 poll "
                f"{_top100_poll_status_for_heartbeat()}")
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
                        #
                        # 17 Sep 2026 restart-resilience fix: the attempt
                        # counter used to persist to disk BEFORE the lock-
                        # acquisition check below. A hard-killed container
                        # (a Railway deploy cutover mid-scan) orphans the
                        # "nightly" job lock for up to _JOB_LOCK_STALE_
                        # SECONDS (3h - see that constant's own docstring),
                        # and every tick that found the lock still held was
                        # ALSO burning a real attempt on nothing but a
                        # "skipped - another process already holds the
                        # lock" no-op - silently exhausting the whole day's
                        # 3-attempt budget within minutes of the restart,
                        # with zero actual scan attempts having run (the
                        # 16-17 Sep 2026 incident: two lock-contention
                        # skips a minute apart burned attempts 1 and 2, and
                        # nothing touched the "nightly" job again for the
                        # rest of that night). Persisting only AFTER a
                        # successful lock acquisition means a lock-
                        # contention tick no longer counts against the
                        # budget at all - only a tick that actually starts
                        # a scan does. The log line text/order for a
                        # successful start is unchanged, so a normal
                        # night's logs stay byte-identical to before this
                        # fix; this budget is now also SHARED with the new
                        # catch-up block below (same state key), so the
                        # two together still cap this process at 3 real
                        # nightly-scan attempts per UTC day.
                        #
                        # Audit fix 2.8: cross-process lock, on top of the
                        # in-process state-file guard above - see
                        # _acquire_job_lock's docstring.
                        if _acquire_job_lock("nightly", log):
                            try:
                                state["scan_attempts"] = {today: n_today + 1}
                                state["last_scan_date"] = today
                                _save_state(state)
                                log(f"[scheduler] starting nightly scans ({', '.join(due)}) "
                                    f"[attempt {n_today + 1}/3 today]")
                                _record_job(
                                    "nightly", log,
                                    lambda lg: _run_nightly({**cfg, "universes": due}, lg),
                                )
                            finally:
                                _release_job_lock("nightly")
                        else:
                            log("[scheduler] nightly scan skipped - another process "
                                "already holds the lock")

                # 17 Sep 2026 restart-resilience fix: catch-up scan for a
                # universe that was SCHEDULED for tonight (see
                # _universes_missing_today's own docstring for exactly
                # what "scheduled" means) but never got a full scan saved
                # - a restart mid-scan, or a tick that lost its attempt to
                # lock contention before the fix above. Runs every tick,
                # same as the due-scan block above (so it fires on
                # scheduler startup too, not just "the regular tick" -
                # startup IS a tick, the first one). Shares the SAME
                # "nightly" job lock and the SAME persisted scan_attempts
                # budget as the due-scan block immediately above (not a
                # separate counter) - together they cap this process at 3
                # real nightly-scan attempts per UTC day, catch-up or not,
                # surviving any number of restarts in between since
                # scan_attempts lives in scheduler_state.json on the
                # Railway Volume. Only fires within CATCHUP_WINDOW_HOURS of
                # the scan-hour instant it's catching up for (see
                # _catchup_reference_night's own docstring for the UTC-
                # midnight-safe window check) - an unrelated restart long
                # after that window has closed does nothing here. A
                # normal, uninterrupted night has nothing missing
                # (_universes_missing_today returns []) on every tick this
                # runs, so this block logs nothing and does nothing on
                # such a night - byte-identical to before this feature
                # existed. Reuses _run_nightly (via _record_job, exactly
                # like the due-scan block) for the catch-up itself, so the
                # normal post-scan steps - derived universes, the reprice
                # pass, sector top-up, alerts - all run for what was
                # caught up exactly as they do for a normal scan; this is
                # not a parallel copy of that orchestration.
                ref_night = _catchup_reference_night(cfg, now)
                if ref_night is not None:
                    missing = _universes_missing_today(cfg, ref_night)
                    if missing:
                        state = _load_state()
                        attempts = state.get("scan_attempts", {})
                        n_today = attempts.get(today, 0)
                        if n_today < 3:
                            if _acquire_job_lock("nightly", log):
                                try:
                                    state["scan_attempts"] = {today: n_today + 1}
                                    state["last_scan_date"] = today
                                    _save_state(state)
                                    log(f"[scheduler] catch-up scan for {ref_night} "
                                        f"({', '.join(missing)}) "
                                        f"[attempt {n_today + 1}/3 today]")
                                    # Commit H: explicitly credited to
                                    # ref_night (the night being caught
                                    # up), not to "now" - this run is
                                    # starting well after that night's own
                                    # scan hour, possibly past midnight
                                    # itself, so _run_nightly's own default
                                    # (today's date at the moment IT
                                    # starts) would be wrong here.
                                    _record_job(
                                        "nightly", log,
                                        lambda lg: _run_nightly(
                                            {**cfg, "universes": missing}, lg, run_night=ref_night),
                                    )
                                finally:
                                    _release_job_lock("nightly")
                            else:
                                log("[scheduler] catch-up scan skipped - another "
                                    "process already holds the lock")

                # AI-readiness roadmap Phase 5: nightly (every day, unlike
                # the weekly digest below), one calendar-day-per-run guard
                # exactly like the nightly scan's own state-then-lock
                # pattern above.
                #
                # 18 Sep 2026 retry fix (mirroring the nightly scan's own
                # 17 Sep restart-resilience fix): last_watchdog_date used
                # to persist BEFORE the job ran, so a failed or killed
                # watchdog run was marked "done" for the day and never
                # retried until tomorrow night. Now persists ONLY once
                # _run_watchdog returns without raising (it no longer
                # swallows its own failure - see its own docstring), with
                # a persisted _DAILY_JOB_RETRY_CAP-attempts-per-day cap
                # (watchdog_attempts, same state file/shape as the
                # nightly scan's own scan_attempts) so a persistently
                # failing night retries once next tick rather than
                # hammering the lock forever. The attempt is only counted
                # once the lock is actually acquired - a lock-contention
                # skip doesn't burn budget, same reasoning as the nightly
                # scan's own attempt counter.
                if (now.hour >= cfg["watchdog_hour"]
                        and state.get("last_watchdog_date") != today):
                    attempts = state.get("watchdog_attempts", {})
                    n_today = attempts.get(today, 0)
                    if n_today < _DAILY_JOB_RETRY_CAP:
                        if _acquire_job_lock("watchdog", log):
                            try:
                                state = _load_state()
                                state["watchdog_attempts"] = {today: n_today + 1}
                                _save_state(state)
                                log(f"[scheduler] starting portfolio watchdog "
                                    f"[attempt {n_today + 1}/{_DAILY_JOB_RETRY_CAP} today]")
                                _record_job("watchdog", log, _run_watchdog)
                                state = _load_state()
                                state["last_watchdog_date"] = today
                                _save_state(state)
                            except Exception as e:
                                log(f"[scheduler] portfolio watchdog failed: {e}")
                            finally:
                                _release_job_lock("watchdog")
                        else:
                            log("[scheduler] portfolio watchdog skipped - another process "
                                "already holds the lock")

                # Trading Cost tab, Commit 1: the two mid-session quote-
                # recorder slots - one per market, each firing in ITS
                # OWN local time (quote_recorder.is_due_now() converts
                # `now` to Australia/Sydney or America/New_York
                # internally; every other job in this file checks a
                # single UTC hour, which can't express "13:00 in two
                # different timezones that drift relative to UTC across
                # DST" - see that function's own docstring). Same one-
                # calendar-day-per-run guard, same persist-only-on-
                # success contract, as the watchdog block above -
                # `today` here is still the UTC calendar day (same
                # `today` the rest of this loop iteration uses), which
                # is fine as a once-per-day guard key even though the
                # due CHECK itself is local-time: the local sampling
                # window (13:00-16:00) never spans a UTC-midnight
                # boundary for either market, so one UTC-dated guard per
                # local calendar day is exact, not approximate.
                if (quote_recorder is not None
                        and quote_recorder.is_due_now(quote_recorder.MARKET_ASX, now=now)
                        and state.get("last_quote_recorder_asx_date") != today):
                    attempts = state.get("quote_recorder_asx_attempts", {})
                    n_today = attempts.get(today, 0)
                    if n_today < _DAILY_JOB_RETRY_CAP:
                        if _acquire_job_lock("quote_recorder_asx", log):
                            try:
                                state = _load_state()
                                state["quote_recorder_asx_attempts"] = {today: n_today + 1}
                                _save_state(state)
                                log(f"[scheduler] starting ASX quote recorder "
                                    f"[attempt {n_today + 1}/{_DAILY_JOB_RETRY_CAP} today]")
                                _record_job("quote_recorder_asx", log, _run_quote_recorder_asx)
                                state = _load_state()
                                state["last_quote_recorder_asx_date"] = today
                                _save_state(state)
                            except Exception as e:
                                log(f"[scheduler] ASX quote recorder failed: {e}")
                            finally:
                                _release_job_lock("quote_recorder_asx")
                        else:
                            log("[scheduler] ASX quote recorder skipped - another process "
                                "already holds the lock")

                if (quote_recorder is not None
                        and quote_recorder.is_due_now(quote_recorder.MARKET_US, now=now)
                        and state.get("last_quote_recorder_us_date") != today):
                    attempts = state.get("quote_recorder_us_attempts", {})
                    n_today = attempts.get(today, 0)
                    if n_today < _DAILY_JOB_RETRY_CAP:
                        if _acquire_job_lock("quote_recorder_us", log):
                            try:
                                state = _load_state()
                                state["quote_recorder_us_attempts"] = {today: n_today + 1}
                                _save_state(state)
                                log(f"[scheduler] starting US quote recorder "
                                    f"[attempt {n_today + 1}/{_DAILY_JOB_RETRY_CAP} today]")
                                _record_job("quote_recorder_us", log, _run_quote_recorder_us)
                                state = _load_state()
                                state["last_quote_recorder_us_date"] = today
                                _save_state(state)
                            except Exception as e:
                                log(f"[scheduler] US quote recorder failed: {e}")
                            finally:
                                _release_job_lock("quote_recorder_us")
                        else:
                            log("[scheduler] US quote recorder skipped - another process "
                                "already holds the lock")

                # Mega-batch Part 10: nightly off-site DB backup - same
                # one-calendar-day-per-run guard as the watchdog above,
                # deliberately its own hour (after both the scan and
                # watchdog hours - see BACKUP_UTC_HOUR's own docstring).
                #
                # 18 Sep 2026 retry fix (mirroring the nightly scan's own
                # 17 Sep restart-resilience fix): last_backup_date used to
                # persist BEFORE the job ran, so a failed or killed backup
                # was marked "done" for the day and never retried until
                # tomorrow night - the exact failure mode a nightly
                # off-site backup can least afford. Now persists ONLY
                # once _run_backup returns without raising (it no longer
                # swallows its own failure - see its own docstring), with
                # a persisted _DAILY_JOB_RETRY_CAP-attempts-per-day cap
                # (backup_attempts, same state file/shape as the nightly
                # scan's own scan_attempts) so a genuinely broken
                # Mailgun/backup night retries once next tick rather than
                # hammering the lock forever. The attempt is only counted
                # once the lock is actually acquired - a lock-contention
                # skip doesn't burn budget, same reasoning as the nightly
                # scan's own attempt counter.
                if (now.hour >= cfg["backup_hour"]
                        and state.get("last_backup_date") != today):
                    attempts = state.get("backup_attempts", {})
                    n_today = attempts.get(today, 0)
                    if n_today < _DAILY_JOB_RETRY_CAP:
                        if _acquire_job_lock("backup", log):
                            try:
                                state = _load_state()
                                state["backup_attempts"] = {today: n_today + 1}
                                _save_state(state)
                                log(f"[scheduler] starting off-site DB backup "
                                    f"[attempt {n_today + 1}/{_DAILY_JOB_RETRY_CAP} today]")
                                _record_job("backup", log, _run_backup)
                                state = _load_state()
                                state["last_backup_date"] = today
                                _save_state(state)
                            except Exception as e:
                                log(f"[scheduler] db backup failed: {e}")
                            finally:
                                _release_job_lock("backup")
                        else:
                            log("[scheduler] DB backup skipped - another process "
                                "already holds the lock")

                # Mega-batch Part 16: nightly Volume usage check +
                # retention prune - same one-calendar-day-per-run guard,
                # its own hour/lock so it never collides with the backup
                # job above even though they default to the same hour.
                if (now.hour >= cfg["volume_check_hour"]
                        and state.get("last_volume_check_date") != today):
                    state = _load_state()
                    state["last_volume_check_date"] = today
                    _save_state(state)
                    if _acquire_job_lock("volume_check", log):
                        try:
                            log("[scheduler] starting volume usage check + retention prune")
                            _record_job("volume_check", log, _run_volume_check)
                        finally:
                            _release_job_lock("volume_check")
                    else:
                        log("[scheduler] volume check skipped - another process "
                            "already holds the lock")

                # Top 100 tab, Commit 1: nightly selection + AI-scoring
                # batch poll/submit - same one-calendar-day-per-run
                # guard, its own hour/lock (defaults to the same hour
                # as backup/volume_check above, never colliding since
                # each job has its own lock).
                if (now.hour >= cfg["top100_hour"]
                        and state.get("last_top100_date") != today):
                    state = _load_state()
                    state["last_top100_date"] = today
                    _save_state(state)
                    if _acquire_job_lock("top100", log):
                        try:
                            log("[scheduler] starting Top 100 selection + AI-scoring batch")
                            _record_job("top100", log, _run_top100)
                        finally:
                            _release_job_lock("top100")
                    else:
                        log("[scheduler] Top 100 job skipped - another process "
                            "already holds the lock")

                # Top 100 Commit 2 (25 Sep 2026, owner-reported): hourly
                # poll for a pending AI-scoring batch, independent of
                # the once-a-day job above - see _run_top100_poll's own
                # docstring for why (a batch otherwise sits un-ingested
                # for up to 24h, and blocks the next night's submission
                # the whole time it does). Cheap when nothing is pending
                # at all - top100_store.get_batch_state() is a single
                # sqlite read, checked BEFORE taking any lock, so an
                # idle Top 100 pipeline (the common case once the
                # backlog is cleared) never even attempts the lock.
                # Uses the SAME "top100" lock as the nightly job above
                # (deliberately NOT a separate lock name) - both jobs
                # read/write the identical top100_batch_state row and
                # the identical in-flight Anthropic batch, and letting
                # them run concurrently could race: the nightly job's
                # own submit_nightly_batch() writing a brand-new batch
                # row while this poll is mid-ingest on the OLD batch
                # could end with this poll's own clear_batch_state()
                # deleting the new row it never saw. Sharing the lock
                # makes that impossible by construction. _record_job()
                # itself still uses the distinct "top100_poll" name
                # (own hard-timeout ceiling, own Admin Dashboard row) so
                # this frequent, usually-uneventful poll never overwrites
                # the nightly job's own last-run status - lock_name=
                # "top100" is passed explicitly (URGENT Commit 3) so its
                # own periodic heartbeat refresh keeps touching the lock
                # ACTUALLY held ("top100"), not a phantom "top100_poll"
                # lock file nothing else ever checks.
                #
                # URGENT (25 Sep 2026, owner-reported): the original
                # "has the wall-clock hour STRING changed since the last
                # successful poll" gate never fired once in production
                # across a stable 3+ hour window despite a genuinely
                # pending batch and an otherwise healthy scheduler (the
                # ASX quote recorder ran normally in that same window).
                # A clean multi-hour simulation of the OLD condition in
                # isolation fires correctly every hour, so no reproducible
                # logic bug was found by tracing it alone - but the
                # design itself has two real weaknesses regardless of
                # the exact production mechanism: (1) it only advances
                # by comparing to the LAST SUCCESSFUL poll, so any
                # single tick where _record_job() raised/timed out (or
                # never even ran, e.g. a lock held by a long-running
                # nightly job blocking this SAME thread for a while)
                # left no record that an ATTEMPT happened, and (2) nothing
                # about it ever forces an IMMEDIATE check right after a
                # fresh boot - recovery after a deploy could always be
                # gated behind however much of the current hour had
                # already elapsed. Replaced with two independent checks,
                # either of which is enough to poll:
                #   - _top100_poll_due(): >=_TOP100_POLL_MIN_INTERVAL_
                #     SECONDS (60min) since the last poll ATTEMPT
                #     (successful or not - the timestamp is persisted
                #     BEFORE _record_job runs, not after, so a failed/
                #     timed-out attempt still counts and the interval
                #     still advances rather than hammering every tick),
                #     stored in scheduler_state.json so it survives a
                #     restart (never reset by boot, per the task's own
                #     explicit requirement).
                #   - `not _top100_boot_poll_attempted` (a plain, non-
                #     persisted module global - see its own docstring):
                #     True until THIS process's own _loop() has made one
                #     attempt, so a fresh boot/deploy always gets an
                #     immediate shot at a pending batch within the next
                #     tick (<=60s), regardless of how recently a
                #     PREVIOUS boot happened to poll - exactly what
                #     collects the currently-pending batch right after
                #     this fix deploys, without waiting on the 60-minute
                #     interval or any wall-clock alignment at all.
                _last_top100_poll_attempt = state.get("last_top100_poll_attempt_at")
                _top100_poll_due = (
                    _last_top100_poll_attempt is None
                    or (time.time() - _last_top100_poll_attempt) >= _TOP100_POLL_MIN_INTERVAL_SECONDS
                    or not _top100_boot_poll_attempted
                )
                if (top100_store is not None
                        and _top100_poll_due
                        and top100_store.get_batch_state() is not None):
                    if _acquire_job_lock("top100", log):
                        try:
                            _top100_boot_poll_attempted = True
                            state = _load_state()
                            state["last_top100_poll_attempt_at"] = time.time()
                            _save_state(state)
                            log("[scheduler] starting hourly Top 100 batch poll")
                            _record_job("top100_poll", log, _run_top100_poll, lock_name="top100")
                        except Exception as e:
                            log(f"[scheduler] hourly Top 100 batch poll failed: {e}")
                        finally:
                            _release_job_lock("top100")
                    else:
                        log("[scheduler] hourly Top 100 batch poll skipped - another "
                            "process already holds the lock")

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
                    if _acquire_job_lock("earnings_refresh", log):
                        try:
                            log("[scheduler] starting earnings calendar refresh")
                            _record_job("earnings_refresh", log, _run_earnings_refresh)
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
                    if _acquire_job_lock("digest", log):
                        try:
                            log(f"[scheduler] starting weekly digest (forced: {force})")
                            _record_job("digest", log, _run_digest)
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
                    if _acquire_job_lock("digest", log):
                        try:
                            log("[scheduler] starting weekly digest")
                            _record_job("digest", log, _run_digest)
                        finally:
                            _release_job_lock("digest")
                    else:
                        log("[scheduler] weekly digest skipped - another process "
                            "already holds the lock")
        except BaseException as e:
            # CRITICAL (25 Sep 2026, owner-reported): BaseException, not
            # Exception - this tick's try block calls out to real
            # network clients (poll_and_ingest_batch's Anthropic client
            # among them) whose async internals can raise
            # asyncio.CancelledError, which has inherited directly from
            # BaseException (not Exception) since Python 3.8 and would
            # otherwise slip straight past this handler and kill this
            # daemon thread on the spot - silently, since a background
            # thread's default uncaught-exception behavior is a stderr
            # traceback with no "[scheduler]" prefix, easy to miss in a
            # log search for that prefix (exactly what this task
            # reported: 80+ minutes, zero scheduler/poll/job lines).
            # Safe to catch this broadly here specifically because this
            # is a daemon thread (see start()'s Thread(..., daemon=True))
            # - the only two BaseException subclasses that would ever
            # legitimately want to end this loop, SystemExit and
            # KeyboardInterrupt, are raised by the interpreter in the
            # MAIN thread during shutdown, never delivered into this
            # thread's own call stack, so there is nothing here that
            # SHOULD end the loop early; process exit already kills a
            # daemon thread regardless of what it's doing. Full
            # traceback, not just str(e), so the next incident's actual
            # exception type/site is visible without needing to
            # reproduce it - str(e) alone was already useless for this
            # exact incident when the user asked "what was the tick-1
            # exception": this log line had never once fired.
            log(f"[scheduler] loop error (tick continues): {e!r}\n"
                f"{traceback.format_exc()}")
        time.sleep(_CHECK_EVERY_SECONDS)


_scheduler_thread = None
_scheduler_start_lock = threading.Lock()


def start(log=print):
    """Start the scheduler daemon thread. Idempotent PER PROCESS - guarded
    by a module-level thread handle + lock, not just app.py's
    st.cache_resource.

    18 Sep 2026 boot-time fix: server.py's FastAPI startup (lifespan())
    now also calls this directly, at container boot, well before any
    Streamlit session exists - see that call site's own comment for the
    production incident this closes (missed 23:00 backups three nights
    running; the lock-fix deploy this same week sat idle for its first
    ~15 minutes until a human happened to open a page - the Streamlit
    subprocess server.py launches doesn't actually execute app.py's
    script, including its own call to this function, until a browser
    session connects). st.cache_resource only wraps app.py's OWN call
    site - it has no idea server.py might already have started the
    scheduler in this same process (or that app.py's own call could fire
    again later if Streamlit re-executes the script after that cache is
    ever cleared) - so this function guards itself instead of relying on
    either caller to know about the other. Whichever call happens first
    wins; every later call in the SAME process is a no-op that returns
    that same thread.

    This can't (and doesn't need to) stop server.py's process and the
    separate Streamlit subprocess process from each independently
    running their own scheduler thread once a session does eventually
    open - those are two different OS processes, so no in-process flag
    reaches across them. That's already safe: every actual job
    (nightly/watchdog/backup/etc.) is additionally gated by the existing
    cross-process _acquire_job_lock()/_release_job_lock() file locks on
    the shared Railway Volume, the same mechanism that already lets more
    than one process tick this same loop without double-running a job."""
    global _scheduler_thread
    with _scheduler_start_lock:
        if _scheduler_thread is not None and _scheduler_thread.is_alive():
            return _scheduler_thread
        _scheduler_thread = threading.Thread(
            target=_loop, args=(log,), daemon=True, name="sdd-scheduler")
        _scheduler_thread.start()
        return _scheduler_thread


def start_with_retry(log=print, initial_delay_seconds=5, max_delay_seconds=300):
    """Like start(), but for the two real call sites (server.py's
    FastAPI lifespan, app.py's own Streamlit script execution) that
    used to guard this call with `with suppress(Exception):` /
    `except Exception: return None` - CRITICAL (25 Sep 2026,
    owner-reported): either pattern means a `start()` failure (thread
    creation itself raising, not a tick inside the thread - this
    function's job lock/state file open, thread-count exhaustion, etc.)
    produces ZERO trace anywhere: no exception, no log line, nothing -
    completely indistinguishable from a healthy, idle scheduler. This
    is the single most likely place for "the scheduler isn't running"
    to happen invisibly, since it's the one call in the whole chain
    that ran with NO logging at all on failure, in either process.

    Retries start() with exponential backoff (capped at
    max_delay_seconds) on a short-lived daemon thread of its own -
    separate from the actual scheduler "sdd-scheduler" thread start()
    itself creates - so the caller (server.py's async lifespan, which
    must not block other startup steps; app.py's own script execution,
    which must not block page load for every session) is never blocked
    waiting on it. Logs every single failed attempt loudly, with a full
    traceback, before retrying - never silently gives up. Once start()
    succeeds this thread's job is done and it exits; start() itself is
    still the one guarding against starting a second "sdd-scheduler"
    thread in the same process (see its own docstring)."""
    def _attempt():
        delay = initial_delay_seconds
        attempt = 0
        while True:
            attempt += 1
            try:
                start(log=log)
                return
            except Exception:
                log(f"[scheduler] start() failed (attempt {attempt}), "
                    f"retrying in {delay}s:\n{traceback.format_exc()}")
                time.sleep(delay)
                delay = min(delay * 2, max_delay_seconds)

    threading.Thread(target=_attempt, daemon=True, name="sdd-scheduler-starter").start()
