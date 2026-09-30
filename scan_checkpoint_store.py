"""
scan_checkpoint_store.py

Commit 5 (27 Sep 2026, owner-directed): lets run_universe_scan() resume
a killed mid-scan run from where it left off instead of restarting from
ticker 0 - the exact gap the 27 Sep Russell 2000 incident exposed twice
in one night (the original 01:20:35 UTC deploy killed it at 1775/1957;
the catch-up restart threw all of that away and hit Yahoo's own
throttle again from ticker 1; Commit 1's own deploy later killed the
RECOVERED catch-up attempt again at 650/1957).

Deliberately separate from scan_store.py: scan_store is what every live
reader (Scanner, Deep Dive, Top 100 selection) treats as "this
universe's current scan", and SCAN_COMPLETENESS_THRESHOLD exists
specifically to stop a partial run from ever reaching it. A checkpoint
here is read ONLY by run_universe_scan() itself, at the very start of
its own per-ticker loop - no live reader, and no other job, ever sees
it. One JSON file per universe, overwritten in place (never appended,
never a history) - same volume-resolution + atomic-write convention
(RAILWAY_VOLUME_MOUNT_PATH, temp file + os.replace) as scan_store.py/
source_health_store.py.

RESUME IS GATED THREE WAYS (see is_resumable() below) so a resumed run
never mixes two sessions' prices in a way that isn't honestly bounded:
  1. The freshly-resolved ticker list must be BYTE-IDENTICAL to the one
     the checkpoint was built against - any mid-scan membership change
     (a corporate action, a source re-fetch landing differently)
     discards it rather than risk misaligning resume_index against a
     different list.
  2. The checkpoint's own run_night must match the run_night THIS call
     was given. scheduler_engine.py only ever passes a run_night value
     it already derived through its own catch-up-window logic
     (_catchup_reference_night()) or "today" for a regular due-scan -
     so this comparison is exactly "is this still the same scan night
     the catch-up logic itself would consider valid", without this
     module needing to import scheduler_engine.py or duplicate its
     window math.
  3. A CHECKPOINT_MAX_AGE_HOURS backstop (belt-and-suspenders, on top
     of #2) - a same-run_night checkpoint whose last write is older
     than this is still discarded; catches a stale checkpoint left
     behind by anything unusual #2 alone wouldn't (an outage spanning
     the boundary, a bug). Set to 2h, not a looser value: the nightly
     scan window (00:00-02:00 UTC) falls at 10am-midday Sydney time,
     while the ASX is open - a resume allowed to reach across too many
     hours could cross the 4pm AEST/AEDT close and save one scan
     holding some tickers at intraday prices and the rest at the
     close, presented as a single snapshot. US universes aren't
     exposed to this (their market is closed during the scan window
     either way), but one cap for every universe is simpler than a
     market-aware one, and 2h still comfortably covers the real case
     this whole feature exists for - a deploy kill resumed within
     minutes, not hours.

RateLimitCircuitBreaker abort (audit fixes Commit 2, 30 Sep 2026,
owner-directed - supersedes this module's original 27 Sep decision):
the checkpoint IS kept when run_universe_scan() aborts on the rate-
limit circuit breaker, specifically so the post-cooldown retry can
resume from it instead of restarting the whole universe from ticker 0
- losing most of a large universe's already-completed progress to a
throttle cost more than the residual risk the three resume gates above
don't already cover (a 45-minute cooldown, RATE_LIMIT_COOLDOWN_MINUTES
in scheduler_engine.py, is well under CHECKPOINT_MAX_AGE_HOURS, and the
same-ticker-list/same-run_night checks still apply exactly as they do
for any other resume). The ORIGINAL reasoning this replaced - "never
resume INTO the same throttle" - is why gate #3's 2h cap and the 45-
minute cooldown exist as real, separate safety margins rather than
relying on "just clear it" alone. clear() is still called on every
NORMAL completion of the per-ticker loop (successful or degraded) - a
finished loop has nothing left to resume, whether or not scan_store.
save_scan() itself goes on to skip the save.
"""

import json
import os
import re
from datetime import datetime, timezone

CHECKPOINT_MAX_AGE_HOURS = 2


def _data_dir():
    base = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH") or os.path.dirname(__file__)
    path = os.path.join(base, "scan_checkpoints")
    os.makedirs(path, exist_ok=True)
    return path


def _slug(universe):
    return re.sub(r"[^a-z0-9]+", "_", universe.lower()).strip("_")


def _path(universe):
    return os.path.join(_data_dir(), f"{_slug(universe)}.json")


def save(universe, run_night, session_started_at, resume_index, tickers, rows, skipped_no_price):
    """Overwrites `universe`'s checkpoint - `resume_index` is the loop
    index of the NEXT ticker still to attempt (i.e. every ticker before
    it, in `tickers`' own order, has already been attempted - captured
    into a row, rejected, or skipped - and must never be re-attempted
    on resume). `rows` is the exact accumulated row list so far, saved
    verbatim so a resume never re-fetches anything already captured.
    Called periodically from inside the per-ticker loop, at the same
    cadence as that loop's own progress-log line. Never raises past a
    filesystem error - a checkpoint write that fails degrades to "this
    run can't be resumed if killed", never to a broken scan."""
    record = {
        "universe": universe,
        "run_night": run_night,
        "session_started_at": session_started_at,
        "last_checkpoint_at": datetime.now(timezone.utc).isoformat(),
        "tickers": tickers,
        "resume_index": resume_index,
        "rows": rows,
        "skipped_no_price": skipped_no_price,
    }
    try:
        path = _path(universe)
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(record, f)
        os.replace(tmp, path)
    except OSError:
        pass


def load(universe):
    """The stored checkpoint for `universe`, or None if there is none
    (or the file is corrupt/unreadable - never raises)."""
    try:
        with open(_path(universe)) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def clear(universe):
    """Deletes `universe`'s checkpoint, if any. Never raises - a
    missing file is exactly the state this is trying to reach."""
    try:
        os.remove(_path(universe))
    except OSError:
        pass


def is_resumable(checkpoint, tickers, run_night, log=print):
    """True if `checkpoint` (from load()) may be resumed against a
    fresh run whose own resolved ticker list is `tickers` and whose own
    run_night is `run_night` - see the module docstring's three gates.
    Logs exactly why, either way, so a discarded checkpoint's reason is
    always visible in Railway logs rather than a silent fresh restart
    that looks identical to "there was never a checkpoint at all"."""
    universe = checkpoint.get("universe")
    if checkpoint.get("tickers") != tickers:
        log(f"[nightly_scan] {universe}: checkpoint discarded - resolved ticker list no "
            f"longer matches (was {len(checkpoint.get('tickers') or [])} tickers, now "
            f"{len(tickers)}) - starting this scan fresh.")
        return False
    if checkpoint.get("run_night") != run_night:
        log(f"[nightly_scan] {universe}: checkpoint discarded - it belongs to run_night "
            f"{checkpoint.get('run_night')!r}, this run is {run_night!r} - starting this "
            f"scan fresh rather than mixing two scan nights.")
        return False
    try:
        last_at = datetime.fromisoformat(checkpoint["last_checkpoint_at"])
        age_hours = (datetime.now(timezone.utc) - last_at).total_seconds() / 3600.0
    except (KeyError, ValueError, TypeError):
        log(f"[nightly_scan] {universe}: checkpoint discarded - malformed "
            f"last_checkpoint_at - starting this scan fresh.")
        return False
    if age_hours > CHECKPOINT_MAX_AGE_HOURS:
        log(f"[nightly_scan] {universe}: checkpoint discarded - {age_hours:.1f}h old, over "
            f"the {CHECKPOINT_MAX_AGE_HOURS}h cap - starting this scan fresh rather than "
            f"resuming something this stale.")
        return False
    return True
