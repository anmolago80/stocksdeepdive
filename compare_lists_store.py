"""
compare_lists_store.py

Compare Commit 3 (27 Sep 2026, owner-directed): named, saved Comparison
lists - signed-in only. A signed-in visitor can save the tickers
currently showing on the Comparison page as a named list, then later
load, rename or delete it. Lists belong to the account (identified by
the signed-in Google email from paywall_engine), never a browser/
session.

Checked watchlist_store.py first, per instruction 4: it stores ONE
flat, UNNAMED set of tickers per signed-in email (table
watchlist(email, ticker), PRIMARY KEY (email, ticker)), feeding the
weekly watchlist digest via all_users(). That shape can't express
"several independently named lists per account, each saved/loaded/
renamed/deleted as its own unit" without a breaking schema change - the
primary key would need a list_name, but every existing caller of that
module (the star-toggle watchlist control on Deep Dive, the weekly
digest job) calls add()/remove()/contains()/get_watchlist()/all_users()
with no list_name to give, and changing the key would either break
those calls or force them onto a fake "default" list they were never
designed around. Extending that table risks the live weekly-digest
feature for a UI it never needs to know about.

So: the same STORAGE PATTERN is reused (same shared stocksdeepdive.db
file via the same RAILWAY_VOLUME_MOUNT_PATH resolution, same WAL
journal mode for concurrent Streamlit sessions, same "identity is only
ever the signed-in email, nothing else about the user" convention) -
but in a NEW table, since the shape genuinely differs.
"""

import json
import os
import sqlite3
from datetime import datetime, timezone

import compare_config

_MAX_LIST_NAME_LEN = 60


def _data_dir():
    return os.environ.get("RAILWAY_VOLUME_MOUNT_PATH") or os.path.dirname(__file__)


DB_PATH = os.path.join(_data_dir(), "stocksdeepdive.db")


def _conn():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS compare_lists (
            email TEXT NOT NULL,
            list_name TEXT NOT NULL,
            tickers TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (email, list_name)
        )"""
    )
    return conn


def _clean_tickers(tickers):
    """De-duplicated, order-preserved, capped at the SAME
    compare_config.COMPARE_MAX_TICKERS every other Comparison entry
    point (search bar, "+ add company", a shared URL) already enforces
    - a saved list is just another way tickers reach the Comparison
    page, not a second cap to keep in sync."""
    out = []
    for t in tickers or []:
        t = (t or "").strip().upper()
        if t and t not in out:
            out.append(t)
    return out[:compare_config.COMPARE_MAX_TICKERS]


def list_lists(email):
    """[{"name":..., "tickers": [...], "updated_at": iso_str}, ...] for
    one signed-in user, most-recently-saved first. Empty list for no
    email or no saved lists."""
    if not email:
        return []
    with _conn() as conn:
        rows = conn.execute(
            "SELECT list_name, tickers, updated_at FROM compare_lists "
            "WHERE email = ? ORDER BY updated_at DESC",
            (email,),
        ).fetchall()
    out = []
    for name, tickers_json, updated_at in rows:
        try:
            tickers = json.loads(tickers_json)
        except Exception:
            tickers = []
        out.append({"name": name, "tickers": tickers, "updated_at": updated_at})
    return out


def save_list(email, list_name, tickers):
    """Create or overwrite (same name -> same list, updated) a named
    list for this signed-in user. Returns (ok, error_key_or_None) - the
    error is a short machine key (not user-facing text) so app.py can
    map it through i18n itself, same convention scanner_engine.py's own
    verify_universe_before_save() uses."""
    if not email:
        return False, "not_signed_in"
    name = (list_name or "").strip()
    if not name:
        return False, "name_required"
    if len(name) > _MAX_LIST_NAME_LEN:
        return False, "name_too_long"
    cleaned = _clean_tickers(tickers)
    if not cleaned:
        return False, "no_tickers"
    with _conn() as conn:
        conn.execute(
            "INSERT INTO compare_lists (email, list_name, tickers, updated_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(email, list_name) DO UPDATE SET "
            "tickers = excluded.tickers, updated_at = excluded.updated_at",
            (email, name, json.dumps(cleaned), datetime.now(timezone.utc).isoformat()),
        )
    return True, None


def load_list(email, list_name):
    """The saved ticker list for one name, or None if not found / no
    email / no name."""
    if not email or not list_name:
        return None
    with _conn() as conn:
        row = conn.execute(
            "SELECT tickers FROM compare_lists WHERE email = ? AND list_name = ?",
            (email, list_name),
        ).fetchone()
    if not row:
        return None
    try:
        return json.loads(row[0])
    except Exception:
        return None


def rename_list(email, old_name, new_name):
    """(ok, error_key_or_None). Refuses a collision with another list
    this same user already has under the new name."""
    if not email:
        return False, "not_signed_in"
    new_name = (new_name or "").strip()
    if not new_name:
        return False, "name_required"
    if len(new_name) > _MAX_LIST_NAME_LEN:
        return False, "name_too_long"
    if new_name == old_name:
        return True, None
    with _conn() as conn:
        exists = conn.execute(
            "SELECT 1 FROM compare_lists WHERE email = ? AND list_name = ?",
            (email, new_name),
        ).fetchone()
        if exists:
            return False, "name_taken"
        conn.execute(
            "UPDATE compare_lists SET list_name = ? WHERE email = ? AND list_name = ?",
            (new_name, email, old_name),
        )
    return True, None


def delete_list(email, list_name):
    if not email or not list_name:
        return
    with _conn() as conn:
        conn.execute(
            "DELETE FROM compare_lists WHERE email = ? AND list_name = ?",
            (email, list_name),
        )
