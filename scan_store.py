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

# Stage 1b (3 Oct 2026, Director-directed): FTSE 100/FTSE 250/TSX 60/
# TSX Composite are scanned and valued like any other universe but are
# PRIVATE - visible only on the Admin dashboard until Andrew removes a
# name from this list. New env var PRIVATE_UNIVERSES, comma-separated;
# unset means the code default below; set to an empty string or "none"
# (case-insensitive) means nothing is private. Read in exactly ONE
# helper, is_private_universe(name), so every reader of a saved scan
# makes the same decision the same way - see that function's own
# docstring for the full "default DENY unless explicitly allowed" list
# of readers this task's own report names.
# Stage 1 Japan, Commit B (3 Oct 2026, Director-directed): Nikkei 225/
# TOPIX 500 added to the code default - every Stage 1b privacy rule
# applies to them unchanged (see is_private_universe()'s own docstring).
_DEFAULT_PRIVATE_UNIVERSES = "FTSE 100, FTSE 250, TSX 60, TSX Composite, Nikkei 225, TOPIX 500"


def _private_universe_set():
    raw = os.environ.get("PRIVATE_UNIVERSES")
    if raw is None:
        raw = _DEFAULT_PRIVATE_UNIVERSES
    raw = raw.strip()
    if not raw or raw.lower() == "none":
        return set()
    return {name.strip() for name in raw.split(",") if name.strip()}


def is_private_universe(name):
    """True iff `name` is in the PRIVATE_UNIVERSES set - see this
    module's own header comment for the env var's exact semantics
    (unset -> code default FTSE 100/FTSE 250/TSX 60/TSX Composite;
    ""/"none" -> nothing private). Re-reads the env var on every call
    (cheap - a handful of string ops, never a file/network read) rather
    than caching it at import time, so a changed Railway variable takes
    effect on the next request without a redeploy being required for
    the privacy decision itself (only for a changed NIGHTLY_UNIVERSES,
    which scheduler_engine.py already documents as redeploy-free too)."""
    return (name or "") in _private_universe_set()


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


def load_scan(universe, allow_private=False):
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
    separately to show "fundamentals scan of X - prices updated Y".

    Stage 1b (3 Oct 2026, Director-directed): `allow_private=False` (the
    default) means a private universe (is_private_universe(universe))
    is treated EXACTLY like a universe with no saved scan at all - None,
    before the file is even opened. This is the single enforcement
    point every public-facing reader (Scanner, homepage, digests,
    Compare, snapshot_store, derived universes, select_top100_pool())
    gets for free just by calling load_scan() with its own default; the
    nightly scan's own internal integrity-guard checks and the
    scheduler's due-logic are the only callers that pass
    allow_private=True - see this task's own report for the full
    reader inventory and why each one does or doesn't."""
    if not allow_private and is_private_universe(universe):
        return None
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


def load_scan_raw(universe, allow_private=False):
    """Like load_scan() above, but skips the 72h staleness cutoff -
    returns the stored payload regardless of age (or None if there's no
    file at all, it's corrupt, or it has no rows). Added for the nightly
    reprice pass (Part 34 addendum 34.7, 11 Sep 2026): a universe that
    has gone stale for DISPLAY purposes (load_scan() returning None past
    72h) still has its rows sitting on disk and is exactly what needs
    repricing - reading them for that purpose must not be gated by the
    same freshness check the price refresh exists to fix. Not exposed to
    the Scanner page itself - only nightly_scan.reprice_universe() calls
    this.

    Stage 1b (3 Oct 2026, Director-directed): same allow_private=False
    default/enforcement as load_scan() above - see that function's own
    docstring. nightly_scan.py's own reprice/catch-up/one-off-check call
    sites and the new Admin "Private universes" panel pass
    allow_private=True; top100_engine.py, moat_export.py, admin_data_
    audit.py's bulk scan and every other caller keep the default deny."""
    if not allow_private and is_private_universe(universe):
        return None
    try:
        with open(_path(universe)) as f:
            payload = json.load(f)
    except (OSError, ValueError):
        return None
    if not payload.get("rows"):
        return None
    return payload


def load_scan_meta(universe, allow_private=False):
    """Scheduler due-logic fix (30 Sep 2026, owner-directed, Commit 3 of
    the growth/Top100 freshness fix): generated_at/repriced_at/run_night/
    degraded for `universe`, with NO 72h staleness cutoff (unlike load_
    scan()) and NO rows read/returned at all - a lightweight metadata-
    only read for scheduler_engine._universes_needing_scan()'s own due
    check, which must judge "due" from generated_at (the last full
    fundamentals scan) ALONE, never blended with repriced_at the way
    load_scan()'s own combined freshness reading is.

    That blend exists for DISPLAY (the Scanner page showing "still
    current" for a weekly-cadence universe the nightly reprice pass kept
    priced up to date) - but it's wrong for a SCHEDULING decision: a
    nightly reprice recomputes Long Score from the stored Intrinsic
    Value, never runs a fresh DCF, so a universe that only ever gets
    repriced (never truly rescanned) looked "fresh" under the blended
    reading indefinitely and was never judged due again - the root
    cause of a live-reported "Top 100 still shows the old list" bug (see
    top100_engine.select_top100_pool()'s own freshness-fix comment for
    the selection-side half of that same incident).

    Returns None if there's no file, it's corrupt, or it has no rows
    (same "no rows = doesn't really exist yet" convention load_scan_raw()
    already uses).

    Stage 1b (3 Oct 2026, Director-directed): `allow_private` passed
    straight through to load_scan_raw() - scheduler_engine._universes_
    needing_scan() (the only caller) passes True, since the scheduler
    must be able to judge a PRIVATE universe due for its own scan too -
    see load_scan()'s own docstring for the general enforcement point."""
    payload = load_scan_raw(universe, allow_private=allow_private)
    if not payload:
        return None
    return {
        "generated_at": payload.get("generated_at"),
        "repriced_at": payload.get("repriced_at"),
        "run_night": payload.get("run_night"),
        "degraded": payload.get("degraded"),
    }


def list_saved_universes(include_private=False):
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
    load_scan_raw()'s own fail-soft convention for a single bad file.

    Stage 1b (3 Oct 2026, Director-directed): `include_private=False`
    (the default) drops every name is_private_universe() flags - so
    top100_engine.select_top100_pool()'s own candidate collection
    (which iterates this list) never even sees a private universe's
    name to begin with, same "deny by default" enforcement as load_
    scan()'s own docstring. The Admin "Private universes" panel is the
    one caller that passes True."""
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
        if universe and (include_private or not is_private_universe(universe)):
            out.append(universe)
    return sorted(out)


def find_ticker_row(ticker, allow_private=False):
    """The freshest saved overnight-scan row for `ticker`, across every
    universe with a scan on disk, or None if no fresh scan covers it.

    Built for Comparison (Commit 2, 27 Sep 2026): a ticker a nightly scan
    already covers doesn't need a fresh live fetch just to compare it -
    this is the per-ticker lookup that decision needs, layered on top of
    list_saved_universes()'s existing "which universes exist" listing
    (same iterate-and-check pattern top100_engine.select_top100_pool()
    already uses, for the same reason). Goes through load_scan() (not
    load_scan_raw()) so this only ever returns a row the Scanner page
    itself would actually be showing right now - the same 72h freshness
    cutoff, not a stale scan sitting on disk past it.

    Returns {"row": <the stored dict, nightly_scan.analyze_ticker_lite()
    shape>, "universe": <str>, "generated_at_label": <str>, "age_hours":
    <float>} for the first matching universe, or None. A ticker saved
    under several universes (e.g. BHP.AX in ASX 50/100/200/300) resolves
    to whichever universe list_saved_universes()'s sorted order checks
    first - deterministic run to run, not "most recently scanned" (every
    fresh scan this returns is equally current within the 72h window
    load_scan() already enforces, so there's no real "freshest of the
    fresh" to break the tie on).

    Stage 1b (3 Oct 2026, Director-directed): `allow_private=False` (the
    default) skips every private universe entirely, via list_saved_
    universes()'s/load_scan()'s own matching defaults - this is
    Compare's own scan-row lookup, one of the explicitly-denied readers
    in this task's own report. A ticker that's ALSO saved under a public
    universe (e.g. covered by both a private and a public scan) still
    resolves normally, from the public universe's own row - the private
    row is simply never reached by this default-deny iteration."""
    ticker = (ticker or "").strip().upper()
    if not ticker:
        return None
    for universe in list_saved_universes(include_private=allow_private):
        payload = load_scan(universe, allow_private=allow_private)
        if not payload:
            continue
        for row in payload.get("rows") or []:
            if (row.get("Ticker") or "").strip().upper() == ticker:
                return {
                    "row": row,
                    "universe": universe,
                    "generated_at_label": payload.get("generated_at_label"),
                    "age_hours": payload.get("age_hours"),
                }
    return None


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


# Stage 1a (3 Oct 2026, Director-directed, UK/Canada data layer), T9 -
# Stage 1c (a later, separate task) will use this to sweep saved scan
# rows for an implausible MOS - NOTHING in this codebase calls it yet,
# same "built ahead of its own caller" status as symbol_mapping.py's
# to_yahoo_symbol()/log_no_yahoo_price() from the same stage. Pure, no
# I/O of its own - operates on whatever row list a caller (eventually
# Stage 1c) already has in hand, e.g. from load_scan()/load_scan_raw().
MOS_SWEEP_HIGH = 95.0
MOS_SWEEP_LOW = -500.0


def mos_sweep_guard(rows):
    """Returns the subset of `rows` whose "MOS %" field (this module's
    own saved-scan-row convention - see nightly_scan.py's own `"MOS %":
    round(mos, 1)` write site, a plain percentage number like 25.0, not
    a 0.25 fraction) is implausible: > MOS_SWEEP_HIGH (95%) or <
    MOS_SWEEP_LOW (-500%) - a company genuinely isn't 95%+ undervalued
    or 500%+ overvalued by this site's own DCF/PE-blend methods in any
    legitimate case; a row that far out is far more likely a data/unit
    bug (e.g. exactly the GBp pence-quoting bug this stage's own Step B
    fixes) than a real valuation call.

    A row missing "MOS %" entirely, or carrying a non-numeric value
    there (None, a stale/malformed entry), is never included - this is
    a guard against an implausible NUMBER, not a completeness check;
    "no MOS at all" is a different, already-handled case (NOT RATED/
    AWAITING elsewhere in this codebase) that this function has no
    opinion on. Order of `rows` is preserved; nothing is mutated."""
    out = []
    for row in rows:
        mos = row.get("MOS %")
        if not isinstance(mos, (int, float)):
            continue
        if mos > MOS_SWEEP_HIGH or mos < MOS_SWEEP_LOW:
            out.append(row)
    return out
