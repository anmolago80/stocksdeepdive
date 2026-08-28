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


def save_scan(universe, rows, source_label, attention_lite=True, degraded=False):
    """`degraded` (audit fix 2.3): True when the caller (nightly_scan.
    run_universe_scan) completed for fewer tickers than its own
    completeness threshold - saved anyway only because no better prior
    scan existed to keep instead. Purely informational for now (not yet
    surfaced in the Scanner UI); the load-bearing part of the fix is
    run_universe_scan choosing not to overwrite a good prior scan with a
    worse partial one in the first place."""
    payload = {
        "universe": universe,
        "source": source_label,
        "generated_at": datetime.now(timezone.utc).isoformat(),
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
    as gone (stale rankings are worse than none)."""
    try:
        with open(_path(universe)) as f:
            payload = json.load(f)
    except (OSError, ValueError):
        return None
    try:
        gen = datetime.fromisoformat(payload["generated_at"])
        age_h = (datetime.now(timezone.utc) - gen).total_seconds() / 3600.0
        if age_h > 72:
            return None
        payload["generated_at_label"] = gen.strftime("%d %b %Y, %H:%M UTC")
        payload["age_hours"] = round(age_h, 1)
    except (KeyError, ValueError):
        return None
    if not payload.get("rows"):
        return None
    return payload
