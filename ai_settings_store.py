"""
ai_settings_store.py

Admin-editable knobs for every AI feature in the roadmap
(AI_ROADMAP_stocksdeepdive.md): the per-tier question quotas and the
site-wide monthly spend cap. One row (id=1), upserted from the admin
"AI settings" panel (app.py's _render_ai_admin_panel, next to the
existing Stats popover). Every AI module reads its numbers from here
rather than hardcoding them, so Andrew can tighten/loosen limits from
the running site without a redeploy - same "editable without a
redeploy" spirit as paywall_engine's PAYWALL_ENABLED switch, just for
numbers instead of an on/off flag.

Defaults match the owner's locked-in decisions from the master AI-
readiness instruction: 20 free questions/day, 300/month + 40/day on
Plus, US$25/month site-wide spend cap. A brand-new deploy with no
row yet returns these defaults - get_settings() never returns partial
data, so callers (ai_gate.py) never need to guard against a missing
key.

Same SQLite file / volume-resolution rule as every other store here -
see positions_store.py's docstring for why SQLite over a hand-rolled
JSON file (concurrent readers/writers).
"""

import os
import sqlite3
from datetime import datetime, timezone


def _data_dir():
    return os.environ.get("RAILWAY_VOLUME_MOUNT_PATH") or os.path.dirname(__file__)


DB_PATH = os.path.join(_data_dir(), "stocksdeepdive.db")

DEFAULTS = {
    "free_daily_limit": 20,
    "plus_daily_limit": 40,
    "plus_monthly_limit": 300,
    "monthly_spend_cap_usd": 25.0,
}

# Keys editable from the admin panel / update_settings() - anything else
# passed to update_settings() is silently ignored rather than crashing
# the save button over an unexpected kwarg.
_EDITABLE_KEYS = tuple(DEFAULTS.keys())


def _conn():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS ai_settings (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            free_daily_limit INTEGER NOT NULL,
            plus_daily_limit INTEGER NOT NULL,
            plus_monthly_limit INTEGER NOT NULL,
            monthly_spend_cap_usd REAL NOT NULL,
            updated_at TEXT NOT NULL
        )"""
    )
    return conn


def get_settings():
    """Always returns every key in DEFAULTS - a brand-new deploy (no row
    yet) just returns DEFAULTS itself, no special-casing needed by
    callers."""
    with _conn() as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM ai_settings WHERE id = 1").fetchone()
    if not row:
        return dict(DEFAULTS)
    d = dict(row)
    d.pop("id", None)
    d.pop("updated_at", None)
    return d


def update_settings(**kwargs):
    """Upsert whichever of _EDITABLE_KEYS are passed; unknown kwargs are
    ignored. Reads the current row first so a partial update (e.g. only
    changing the spend cap) never clobbers the other fields back to
    DEFAULTS."""
    current = get_settings()
    for k, v in kwargs.items():
        if k in _EDITABLE_KEYS and v is not None:
            current[k] = v
    with _conn() as conn:
        conn.execute(
            """INSERT INTO ai_settings
                 (id, free_daily_limit, plus_daily_limit, plus_monthly_limit,
                  monthly_spend_cap_usd, updated_at)
               VALUES (1, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                 free_daily_limit = excluded.free_daily_limit,
                 plus_daily_limit = excluded.plus_daily_limit,
                 plus_monthly_limit = excluded.plus_monthly_limit,
                 monthly_spend_cap_usd = excluded.monthly_spend_cap_usd,
                 updated_at = excluded.updated_at""",
            (current["free_daily_limit"], current["plus_daily_limit"],
             current["plus_monthly_limit"], current["monthly_spend_cap_usd"],
             datetime.now(timezone.utc).isoformat()),
        )
    return current
