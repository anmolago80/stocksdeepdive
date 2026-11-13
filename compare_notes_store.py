"""
compare_notes_store.py

TASK 55 (Director-directed, 13 Nov 2026), part C: "My notes" - a
short, private, per-user note attached to a ticker, shown as an extra
column in the Comparison ranking table and in the comparison CSV
download. Signed-in only.

Same STORAGE PATTERN as compare_lists_store.py's own (and that
module's own docstring reasoning for why this is a NEW table rather
than an extension of watchlist_store.py applies here too, for the
same reason): same shared stocksdeepdive.db file via the same
RAILWAY_VOLUME_MOUNT_PATH resolution, same WAL journal mode for
concurrent Streamlit sessions, same "identity is only ever the
signed-in email" convention. A new table, not a new column on
compare_lists - a note is per (user, ticker), independent of which
list(s) that ticker happens to sit in, and must follow the ticker
across EVERY list and every ad-hoc comparison (per the task's own
"follows the ticker in every list and every comparison" instruction) -
a column on compare_lists would instead tie it to one particular
saved list's row, the opposite of what's asked.
"""

import os
import sqlite3
from datetime import datetime, timezone

_MAX_NOTE_LEN = 280


def _data_dir():
    return os.environ.get("RAILWAY_VOLUME_MOUNT_PATH") or os.path.dirname(__file__)


DB_PATH = os.path.join(_data_dir(), "stocksdeepdive.db")


def _conn():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS compare_notes (
            email TEXT NOT NULL,
            ticker TEXT NOT NULL,
            note TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (email, ticker)
        )"""
    )
    return conn


def get_note(email, ticker):
    """The note text for one (email, ticker), or "" if none/no email/
    no ticker. Never None, so a caller can always safely display or
    compare it without a null check."""
    ticker = (ticker or "").strip().upper()
    if not email or not ticker:
        return ""
    with _conn() as conn:
        row = conn.execute(
            "SELECT note FROM compare_notes WHERE email = ? AND ticker = ?",
            (email, ticker),
        ).fetchone()
    return row[0] if row else ""


def get_notes(email, tickers):
    """{ticker: note} for every ticker in `tickers` that has a stored
    note - a bulk read for rendering a whole results table in one
    query instead of one per row. A ticker with no stored note simply
    isn't a key in the returned dict (callers read it via .get(t, "")
    the same way get_note()'s own empty-string default works).
    Empty/no email/no tickers -> {}."""
    tickers = [(t or "").strip().upper() for t in (tickers or []) if (t or "").strip()]
    if not email or not tickers:
        return {}
    placeholders = ",".join("?" for _ in tickers)
    with _conn() as conn:
        rows = conn.execute(
            f"SELECT ticker, note FROM compare_notes WHERE email = ? "
            f"AND ticker IN ({placeholders})",
            (email, *tickers),
        ).fetchall()
    return {ticker: note for ticker, note in rows}


def save_note(email, ticker, note):
    """Create/update/clear the note for one (email, ticker). Returns
    (ok, error_key_or_None) - same machine-key convention as compare_
    lists_store.save_list(). A blank (after stripping) note DELETES
    the row rather than storing an empty string, so a user who clears
    a note doesn't leave a permanent empty-string row behind for every
    ticker they ever typed something into and then erased."""
    ticker = (ticker or "").strip().upper()
    if not email:
        return False, "not_signed_in"
    if not ticker:
        return False, "no_ticker"
    note = (note or "").strip()
    if len(note) > _MAX_NOTE_LEN:
        return False, "note_too_long"
    with _conn() as conn:
        if not note:
            conn.execute(
                "DELETE FROM compare_notes WHERE email = ? AND ticker = ?",
                (email, ticker),
            )
        else:
            conn.execute(
                "INSERT INTO compare_notes (email, ticker, note, updated_at) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(email, ticker) DO UPDATE SET "
                "note = excluded.note, updated_at = excluded.updated_at",
                (email, ticker, note, datetime.now(timezone.utc).isoformat()),
            )
    return True, None
