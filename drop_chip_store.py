"""
drop_chip_store.py

TASK 39/TASK 42 item 6b (Director-directed): one small JSON file per
ticker on the volume, holding the nightly-computed drop_chip_engine.
compute_peak_and_drop() result for a Top 200 pool/ASX-extension
member - the SAME one-JSON-file-per-ticker pattern support_levels_
store.py already establishes (built together with that task, per the
Director's own "reuse TASK 39's 3y-peak data" instruction).

Written ONLY by the nightly support-levels job (support_levels_job.py,
extended to also compute this from the SAME weekly bars it already
resamples - no second download). Read by top100_render.py's own chip/
mini-chart renderer (TASK 39, display only) and top100_engine.py's own
"Quality on sale" candidate filter (TASK 42 item 6b, drop-depth
ranking only) - never fed into any OTHER score/selection anywhere in
this codebase.
"""

import datetime
import json
import os
import tempfile

_STORE_DIR_NAME = "drop_chip"


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
    """Same atomic-write pattern as support_levels_store._atomic_write()
    - tmp file + os.replace() so a crash mid-write never leaves a
    corrupt entry behind. Best-effort: a write failure just means this
    ticker stays exactly as it was (missing, or the previous entry)."""
    try:
        fd, tmp_path = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".tmp_", suffix=".json")
        with os.fdopen(fd, "w") as f:
            json.dump(obj, f, default=str)
        os.replace(tmp_path, path)
    except OSError:
        pass


def save(ticker, result, currency=None, computed_at=None):
    """Writes (or overwrites) this ticker's entry. `result` is
    drop_chip_engine.compute_peak_and_drop()'s own return dict - stored
    as-is (dates serialised to ISO strings via `default=str`), minus
    its own "series" list serialised separately as [[iso_date, close],
    ...] pairs (json.dump's `default=str` only fires on the date
    OBJECTS themselves when they're not already inside a plain list of
    lists - tuples survive json.dump fine either way, but normalising
    to lists here keeps get()'s own reconstruction simple and explicit
    rather than relying on tuple/list round-trip equivalence). `currency`
    is the listing currency this ticker's price/peak figures are in -
    needed by the chip renderer, same convention as support_levels_
    store.py's own `currency` field."""
    payload = {k: v for k, v in result.items() if k != "series"}
    payload["series"] = [[d, c] for d, c in (result.get("series") or [])]
    payload["ticker"] = ticker
    payload["currency"] = currency
    payload["computed_at"] = computed_at or datetime.datetime.now(datetime.timezone.utc).isoformat()
    _atomic_write(_path(ticker), payload)


def get(ticker):
    """Returns the stored entry (dates reconstructed from their ISO
    strings, "series" reconstructed as [(date, close), ...] pairs) or
    None if there's no entry at all, or it's corrupt. Never raises -
    a ticker with no entry yet (cold, or not currently a Top 200 pool/
    ASX-extension member) simply has no chip/chart to show."""
    path = _path(ticker)
    try:
        if not os.path.exists(path):
            return None
        with open(path) as f:
            obj = json.load(f)
        for key in ("peak_date", "current_date"):
            if obj.get(key):
                obj[key] = datetime.date.fromisoformat(obj[key])
        obj["series"] = [
            (datetime.date.fromisoformat(d) if isinstance(d, str) else d, c)
            for d, c in (obj.get("series") or [])
        ]
        return obj
    except Exception:
        return None
