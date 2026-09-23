"""
source_health_store.py

Per-source health tracking for scanner_engine.py's live data fetches:
the last-known-good snapshot (timestamp, row count, full rows) plus the
result of the most recent health-check run against it.

Why this exists: asx300list.com/allordslist.com both passed scanner_
engine.py's existing row-count guard for five years while frozen on a
28 April 2021 snapshot (see scanner_engine.py's own "Index containment"
comments) - a guard that only proves a fetch is well-formed, not that
it's current. This store is what lets a fetcher keep serving its last
GOOD result instead of a silently-stale one the moment a health check
fails, and lets the Admin Dashboard show which source that's actually
happening for right now.

Same volume-resolution rule as scan_store.py: files live on the Railway
Volume when one is attached (RAILWAY_VOLUME_MOUNT_PATH), falling back to
this directory locally. One JSON file per source, atomic write (temp
file + os.replace), same convention as scan_store.py's own save_scan().
"""

import json
import os
import re
from datetime import datetime, timezone


def _data_dir():
    base = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH") or os.path.dirname(__file__)
    path = os.path.join(base, "source_health")
    os.makedirs(path, exist_ok=True)
    return path


def _slug(source):
    return re.sub(r"[^a-z0-9]+", "_", source.lower()).strip("_")


def _path(source):
    return os.path.join(_data_dir(), f"{_slug(source)}.json")


def get(source):
    """The stored health record for `source`, or None if it's never
    been checked (or the file is corrupt). Shape:
    {source, last_good_at, last_good_row_count, last_good_rows: [...],
    stale, last_check_at, last_checks: {name: {"ok":, "detail":}},
    last_failure_reason, consecutive_failures}."""
    try:
        with open(_path(source)) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _save(record):
    tmp = _path(record["source"]) + ".tmp"
    with open(tmp, "w") as f:
        json.dump(record, f)
    os.replace(tmp, _path(record["source"]))
    return record


def record_success(source, rows, checks=None):
    """`rows`: a list of plain dicts (a DataFrame's own
    .to_dict("records") is exactly this shape) - becomes the new
    last-known-good, replacing whatever was there before. `checks`
    (optional): the same {name: {"ok":, "detail":}} shape record_
    failure() takes, stored even on a clean pass so the Admin
    Dashboard can show that checks are actually running, not just
    report them when something's wrong. Clears `stale` and resets the
    failure streak - a source that was down and has now recovered goes
    back to being reported as healthy."""
    record = {
        "source": source,
        "last_good_at": datetime.now(timezone.utc).isoformat(),
        "last_good_row_count": len(rows),
        "last_good_rows": rows,
        "stale": False,
        "last_check_at": datetime.now(timezone.utc).isoformat(),
        "last_checks": checks or {},
        "last_failure_reason": None,
        "consecutive_failures": 0,
    }
    return _save(record)


def record_failure(source, checks, reason):
    """Marks `source` stale WITHOUT touching last_good_at/
    last_good_row_count/last_good_rows - those stay exactly as they
    were from the last real success, which is the entire point (a
    failed fetch/check must never overwrite a good last-known-good with
    a worse one). Returns the updated record; callers should read the
    PRIOR record's own `stale` flag (via get(), before calling this) to
    tell a brand new failure from one that's already been alerted on -
    this function itself doesn't dedupe, so it can't accidentally
    swallow a call site's chance to alert."""
    existing = get(source) or {
        "source": source, "last_good_at": None, "last_good_row_count": None,
        "last_good_rows": [], "stale": False, "consecutive_failures": 0,
    }
    record = dict(existing)
    record["stale"] = True
    record["last_check_at"] = datetime.now(timezone.utc).isoformat()
    record["last_checks"] = checks
    record["last_failure_reason"] = reason
    record["consecutive_failures"] = existing.get("consecutive_failures", 0) + 1
    return _save(record)


def last_good_dataframe(source):
    """The persisted last-known-good rows as a pandas DataFrame, or
    None if nothing has ever succeeded for this source. Deferred pandas
    import - this module otherwise has no reason to depend on it, same
    "plain JSON, no heavy deps at module level" convention scan_store.py
    itself follows."""
    record = get(source)
    if not record or not record.get("last_good_rows"):
        return None
    import pandas as pd
    return pd.DataFrame(record["last_good_rows"])


def list_all(sources):
    """{source_name: record_or_None} for every name in `sources` - the
    Admin Dashboard's source-health table reads this in one call rather
    than one get() per row."""
    return {s: get(s) for s in sources}
