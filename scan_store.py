"""
scan_store.py

Read/write for the overnight universe scans. The nightly job
(scheduler_engine -> nightly_scan.run_universe_scan) writes one JSON file
per universe here; the Scanner page reads it back and shows it instantly,
so a visitor never has to sit through a 30-minute live index scan just to
see a ranking.

Files live on the Railway Volume when one is attached
(RAILWAY_VOLUME_MOUNT_PATH - the same rule as every other persisted file
in this app), falling back to this directory locally. No volume means the
overnight scans vanish on each redeploy but everything still works.
"""

import json
import os
import re
from datetime import datetime, timezone


def _data_dir():
    base = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH") or os.path.dirname(__file__)
    path = os.path.join(base, "overnight_scans")
    os.makedirs(path, exist_ok=True)
    return path


def _slug(universe):
    return re.sub(r"[^a-z0-9]+", "_", universe.lower()).strip("_")


def _path(universe):
    return os.path.join(_data_dir(), f"{_slug(universe)}.json")


def save_scan(universe, rows, source_label, attention_lite=True, degraded=False, run_night=None):
    """`degraded` (audit fix 2.3): True when the caller (nightly_scan.
    run_universe_scan) completed for fewer tickers than its own
    completeness threshold - saved anyway only because no better prior
    scan existed to keep instead. Purely informational for now (not yet
    surfaced in the Scanner UI); the load-bearing part of the fix is
    run_universe_scan choosing not to overwrite a good prior scan with a
    worse partial one in the first place.

    `run_night` (Commit H, 20 Sep 2026): the 'YYYY-MM-DD' UTC date of the
    scheduled scan NIGHT this run belongs to - captured once at the top
    of scheduler_engine._run_nightly() and passed all the way down,
    deliberately independent of `generated_at` (which stays real
    wall-clock completion time, untouched, for every other reader that
    means "how fresh is this data"). Only scheduler_engine.
    _universes_missing_today() reads this field, to tell whether a
    universe scanned tonight was truly credited to its scheduled night -
    a large universe queued after several smaller ones can easily finish
    after 00:00 UTC, and `generated_at` alone would then misattribute it
    to the following day (see that function's own docstring for the
    incident this fixes). None (the default) for a scan saved with no
    scheduler context at all (a hand-run `python nightly_scan.py "X"`,
    or any scan saved before this field existed) - _universes_missing_
    today() falls back to generated_at's date in that case, exactly the
    pre-Commit-H behavior."""
    payload = {
        "universe": universe,
        "source": source_label,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "run_night": run_night,
        "rows": rows,
        "attention_lite": attention_lite,
        "degraded": degraded,
    }
    tmp = _path(universe) + ".tmp"
    with open(tmp, "w") as f:
        json.dump(payload, f)
    os.replace(tmp, _path(universe))
    return payload


def load_scan(universe):
    """The stored overnight scan for `universe`, or None. Adds a
    human-readable freshness label; anything older than 3 days is treated
    as gone (stale rankings are worse than none).

    Part 34 addendum 34.7 (11 Sep 2026): freshness is now judged by
    whichever is MORE RECENT of `generated_at` (the last full fundamentals
    scan) and `repriced_at` (the last nightly price-only refresh, set by
    reprice_scan() below) - without this, a weekly-cadence universe's
    `generated_at` would age past the 72h cutoff on 4 of every 7 days and
    the Scanner would go blank for it, even on nights the reprice pass
    kept its price/MOS/value score genuinely current. `generated_at`
    itself is left completely untouched by this - it still means exactly
    what it always meant ("last full fundamentals scan"), the Scanner
    page's own two-part date line (app.py) reads both timestamps
    separately to show "fundamentals scan of X - prices updated Y"."""
    try:
        with open(_path(universe)) as f:
            payload = json.load(f)
    except (OSError, ValueError):
        return None
    try:
        gen = datetime.fromisoformat(payload["generated_at"])
        repriced = None
        _rp_raw = payload.get("repriced_at")
        if _rp_raw:
            try:
                repriced = datetime.fromisoformat(_rp_raw)
            except ValueError:
                repriced = None
        freshness_ts = max(gen, repriced) if repriced else gen
        age_h = (datetime.now(timezone.utc) - freshness_ts).total_seconds() / 3600.0
        if age_h > 72:
            return None
        payload["generated_at_label"] = gen.strftime("%d %b %Y, %H:%M UTC")
        payload["age_hours"] = round(age_h, 1)
        if repriced:
            payload["repriced_at_label"] = repriced.strftime("%d %b %Y, %H:%M UTC")
    except (KeyError, ValueError):
        return None
    if not payload.get("rows"):
        return None
    return payload


def load_scan_raw(universe):
    """Like load_scan() above, but skips the 72h staleness cutoff -
    returns the stored payload regardless of age (or None if there's no
    file at all, it's corrupt, or it has no rows). Added for the nightly
    reprice pass (Part 34 addendum 34.7, 11 Sep 2026): a universe that
    has gone stale for DISPLAY purposes (load_scan() returning None past
    72h) still has its rows sitting on disk and is exactly what needs
    repricing - reading them for that purpose must not be gated by the
    same freshness check the price refresh exists to fix. Not exposed to
    the Scanner page itself - only nightly_scan.reprice_universe() calls
    this."""
    try:
        with open(_path(universe)) as f:
            payload = json.load(f)
    except (OSError, ValueError):
        return None
    if not payload.get("rows"):
        return None
    return payload


def list_saved_universes():
    """Every universe with a saved overnight scan on disk right now -
    the Top 100 tab's own "merge every universe's stored scan rows"
    selection step (top100_engine.py) reads this rather than a
    hardcoded universe list, so it always matches whatever NIGHTLY_
    UNIVERSES currently produces without needing a second, separately-
    maintained copy of that list here.

    Read from each file's own "universe" field (never re-derived from
    its filename slug via _slug() above, which is lossy/one-way - e.g.
    "S&P 500" and "S and P 500" would collide) - a corrupt/unreadable
    file is skipped rather than failing the whole listing, matching
    load_scan_raw()'s own fail-soft convention for a single bad file."""
    out = []
    try:
        entries = os.listdir(_data_dir())
    except OSError:
        return out
    for fn in entries:
        if not fn.endswith(".json"):
            continue
        try:
            with open(os.path.join(_data_dir(), fn)) as f:
                payload = json.load(f)
        except (OSError, ValueError):
            continue
        universe = payload.get("universe")
        if universe:
            out.append(universe)
    return sorted(out)


def reprice_scan(universe, rows, repriced_count=None, kept_stale_count=None):
    """Part 34 addendum 34.7 (11 Sep 2026): persists a repriced version of
    `universe`'s stored scan - same file, `rows` replaced with the
    freshly-repriced ones (see nightly_scan.reprice_universe()), a new
    `repriced_at` timestamp added, everything else about the payload
    (`generated_at` - the last FULL fundamentals scan - `source`,
    `attention_lite`, `degraded`) carried over unchanged from whatever
    was already on disk. `generated_at` is deliberately NEVER touched
    here - only save_scan() (a real full scan) ever moves it forward.
    Returns None (and writes nothing) if there's no prior scan on disk to
    reprice - the nightly full-scan path is what creates a universe's
    first payload; there's nothing for this function to do before that."""
    existing = load_scan_raw(universe)
    if existing is None:
        return None
    payload = dict(existing)
    payload["rows"] = rows
    payload["repriced_at"] = datetime.now(timezone.utc).isoformat()
    if repriced_count is not None:
        payload["repriced_count"] = repriced_count
    if kept_stale_count is not None:
        payload["repriced_kept_stale_count"] = kept_stale_count
    # Strip any display-only fields load_scan()/load_scan_raw() may have
    # attached on the way in (generated_at_label/age_hours/
    # repriced_at_label) - the file on disk should only ever hold the
    # source-of-truth fields; load_scan() recomputes labels fresh on
    # every read.
    for _k in ("generated_at_label", "age_hours", "repriced_at_label"):
        payload.pop(_k, None)
    tmp = _path(universe) + ".tmp"
    with open(tmp, "w") as f:
        json.dump(payload, f)
    os.replace(tmp, _path(universe))
    return payload


def apply_attention_topup(universe, rows, topped_up_count=None):
    """Discovery drop-and-reweight fix, Part 2 (18 Sep 2026): persists
    `rows` after nightly_scan.run_attention_topup() has recomputed
    Discovery/Long Score with discovery_measured=True for an
    attention_lite universe's top movers. Same carry-over convention as
    reprice_scan() above: `rows` replaces the stored rows, everything
    else about the payload (`generated_at` - the last full fundamentals
    scan - `source`, `attention_lite`, `degraded`) is carried over
    unchanged - this is a partial re-score of rows already scanned
    tonight, not a new full scan, so `generated_at` must never move for
    it (the admin scan calendar / staleness checks both key off it
    meaning "last full scan"). Returns None (writes nothing) if there's
    no prior scan on disk - the top-up always runs right after a fresh
    save_scan() this same night, so this should never actually happen
    in practice."""
    existing = load_scan_raw(universe)
    if existing is None:
        return None
    payload = dict(existing)
    payload["rows"] = rows
    payload["attention_topup_at"] = datetime.now(timezone.utc).isoformat()
    if topped_up_count is not None:
        payload["attention_topup_count"] = topped_up_count
    for _k in ("generated_at_label", "age_hours", "repriced_at_label"):
        payload.pop(_k, None)
    tmp = _path(universe) + ".tmp"
    with open(tmp, "w") as f:
        json.dump(payload, f)
    os.replace(tmp, _path(universe))
    return payload


def invalidate(universe):
    """Deletes the stored scan for `universe`, if any - the file-based
    equivalent of "mark this universe's scan stale so the scheduler
    rescans it on its next tick" (scheduler_engine._universes_needing_scan
    treats a missing file exactly like one that's aged past its
    staleness threshold - see load_scan() above). Fix 9 (2026-09-01):
    used by nightly_scan.cleanup_fix9_nan_data() to invalidate the ASX
    200/ASX 300 scans saved by the 31 Aug 20:00 UTC run (192/197 and
    236/241 rows with a NaN price respectively - see that function's
    docstring for the root cause), so they're rescanned on the next
    scheduler tick rather than left showing garbage until the normal 72h
    staleness cutoff. Not exposed anywhere else - a scan is normally only
    ever replaced by a fresher one via save_scan(), never removed
    outright. Returns True if a file was actually removed, False if there
    was nothing to invalidate."""
    try:
        os.remove(_path(universe))
        return True
    except OSError:
        return False
