"""
support_levels_store.py

TASK 36 (Director-directed, owner-reported, 6 Nov 2026): one small JSON
file per ticker on the volume, holding the nightly-computed support-
levels result (support_levels_engine.compute_support_levels()'s own
return shape) for a Top 200 pool/ASX-extension member - the SAME one-
JSON-file-per-ticker pattern financials_income_store.py already
establishes for this codebase's "small per-ticker data that doesn't
need SQLite's own query/sort machinery" cases (unlike top100_store.py's
SQLite pool table - this store never needs to be queried BY VALUE,
only ever read back by its own ticker, exactly like financials_income_
store.py's own entries).

Written ONLY by the nightly support-levels job (support_levels_job.py),
for Top 200 pool members + ASX extension members ONLY - never for
every scanned ticker (the Director's own explicit instruction). Read
by top100_render.py's own chip renderer, display only - this store is
never fed into any score/selection/ranking anywhere in this codebase.
"""

import datetime
import json
import os
import tempfile

_STORE_DIR_NAME = "support_levels"


def _data_dir():
    return os.environ.get("RAILWAY_VOLUME_MOUNT_PATH") or os.path.dirname(__file__)


def _store_dir():
    path = os.path.join(_data_dir(), _STORE_DIR_NAME)
    try:
        os.makedirs(path, exist_ok=True)
    except OSError:
        pass
    return path


def _safe_name(ticker):
    return "".join(c if (c.isalnum() or c in "._-") else "-" for c in (ticker or "").upper())


def _path(ticker):
    return os.path.join(_store_dir(), f"{_safe_name(ticker)}.json")


def _atomic_write(path, obj):
    """Same atomic-write pattern as financials_income_store._atomic_
    write()/fundamentals_data._write_cache() - tmp file + os.replace()
    so a crash mid-write never leaves a corrupt entry behind. Best-
    effort: a write failure just means this ticker stays exactly as it
    was (missing, or the previous entry)."""
    try:
        fd, tmp_path = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".tmp_", suffix=".json")
        with os.fdopen(fd, "w") as f:
            json.dump(obj, f, default=str)
        os.replace(tmp_path, path)
    except OSError:
        pass


def save(ticker, result, currency=None, computed_at=None):
    """Writes (or overwrites) this ticker's entry. `result` is
    support_levels_engine.compute_support_levels()'s own return dict -
    stored as-is (dates serialised to ISO strings via `default=str` in
    _atomic_write() above), plus `currency` (the listing currency this
    ticker's price/support figures are in - needed by the chip
    renderer to format the price correctly, same convention as every
    other currency-tagged store in this codebase) and `computed_at`
    (defaults to now, UTC) for staleness disclosure if ever needed."""
    payload = dict(result)
    payload["ticker"] = ticker
    payload["currency"] = currency
    payload["computed_at"] = computed_at or datetime.datetime.now(datetime.timezone.utc).isoformat()
    _atomic_write(_path(ticker), payload)


def get(ticker):
    """Returns the stored entry (dates reconstructed from their ISO
    strings) or None if there's no entry at all, or it's corrupt.
    Never raises - the exact "missing data never blocks a decision"
    philosophy as every other store in this codebase; a ticker with no
    entry yet (cold, or not currently a Top 200 pool/ASX-extension
    member) simply has no chip to show."""
    path = _path(ticker)
    try:
        if not os.path.exists(path):
            return None
        with open(path) as f:
            obj = json.load(f)
        for key in ("support_date", "next_support_date", "lowest_since_date"):
            if obj.get(key):
                obj[key] = datetime.date.fromisoformat(obj[key])
        return obj
    except Exception:
        return None
