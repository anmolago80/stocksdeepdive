"""
checklist_store.py

Per-user, per-ticker "pre-purchase checklist" and thesis journal
(Services batch 3, Part B) - judgement tickboxes plus a thesis/verdict
note, one row per (email, ticker), upserted. The COMPUTED half of the
checklist (Moat/Quality/ROIC-vs-WACC/etc.) is never stored here - it's
read live, fresh, from the engines' own already-computed outputs on
every page view (see app.py's _render_checklist_panel), so nothing here
can ever go stale relative to the site's own numbers. Entirely private:
never surfaced on any public page, the API, MCP, or a snapshot - see
this module's own field shape, which has no path into snapshot_store.

WHERE THE FILE LIVES / WHY SQLITE: identical rule to every other store in
this app - the attached Railway Volume when one exists, falling back to
this directory locally (see watchlist_store.py / portfolio_store.py);
SQLite serialises concurrent writers safely, a hand-rolled JSON
read-modify-write does not.

IDENTITY: the signed-in email from paywall_engine.current_user_email(),
exactly like portfolio_store.py - every read/write below takes `email`
as its first argument and scopes its query to exactly that value.

verdict_note vs portfolio_store's thesis field: for a ticker the user
already HOLDS, the checklist's thesis textarea reads/writes through
portfolio_store.update_thesis() instead (the SAME field the AI watchdog's
thesis-check and every other "thesis" reference on the site already use)
- verdict_note is only the fallback home for that same text when the
ticker isn't held yet, so research done before a purchase isn't lost;
see app.py's _render_checklist_panel and _render_add_holding_expander for
how it's carried across automatically once a holding is added.
"""

import json
import os
import sqlite3
from datetime import datetime, timezone


def _data_dir():
    return os.environ.get("RAILWAY_VOLUME_MOUNT_PATH") or os.path.dirname(__file__)


DB_PATH = os.path.join(_data_dir(), "stocksdeepdive.db")

STATUS_VALUES = ("draft", "done")


def _conn():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS checklists (
            email TEXT NOT NULL,
            ticker TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            items_json TEXT NOT NULL DEFAULT '[]',
            verdict_note TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'draft',
            PRIMARY KEY (email, ticker)
        )"""
    )
    return conn


def _row_to_dict(row):
    ticker, items_json, verdict_note, status, created_at, updated_at = row
    try:
        items = json.loads(items_json) or []
    except (TypeError, ValueError):
        items = []
    return {
        "ticker": ticker, "items": items, "verdict_note": verdict_note or "",
        "status": status or "draft", "created_at": created_at, "updated_at": updated_at,
    }


def get_checklist(email, ticker):
    """One (email, ticker) checklist, or None if it's never been saved -
    the caller (app.py's checklist panel) treats None as "start from the
    default judgement-item list", never as an error."""
    if not email or not ticker:
        return None
    with _conn() as conn:
        row = conn.execute(
            "SELECT ticker, items_json, verdict_note, status, created_at, updated_at "
            "FROM checklists WHERE email = ? AND ticker = ?",
            (email, ticker.upper()),
        ).fetchone()
    return _row_to_dict(row) if row else None


def get_checklists_for_tickers(email, tickers):
    """{ticker: checklist_dict} for every ticker in `tickers` that has a
    saved checklist for this email - used by My Portfolio (Part B3) to
    badge every holding row without one query per row. Empty dict for no
    email/tickers, never "everyone's checklists"."""
    tickers = sorted({(t or "").strip().upper() for t in (tickers or []) if t})
    if not email or not tickers:
        return {}
    placeholders = ",".join("?" * len(tickers))
    with _conn() as conn:
        rows = conn.execute(
            f"SELECT ticker, items_json, verdict_note, status, created_at, updated_at "
            f"FROM checklists WHERE email = ? AND ticker IN ({placeholders})",
            (email, *tickers),
        ).fetchall()
    return {r[0]: _row_to_dict(r) for r in rows}


def save_checklist(email, ticker, items, verdict_note="", status="draft"):
    """UPSERT one (email, ticker) checklist. `items` is the full
    judgement-item list (fixed + any user-added ones), saved verbatim as
    [{"text", "checked", "custom"}, ...] - the computed half of the
    checklist is never persisted (see this module's own docstring).
    `verdict_note` is the thesis/notes text for a ticker not (yet) held -
    see app.py's _render_checklist_panel for when this is used instead of
    portfolio_store.update_thesis()."""
    if not email or not ticker:
        return
    ticker = ticker.upper()
    status = status if status in STATUS_VALUES else "draft"
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as conn:
        cur = conn.execute(
            "UPDATE checklists SET items_json = ?, verdict_note = ?, status = ?, "
            "updated_at = ? WHERE email = ? AND ticker = ?",
            (json.dumps(items or []), verdict_note or "", status, now, email, ticker),
        )
        if cur.rowcount == 0:
            conn.execute(
                "INSERT INTO checklists (email, ticker, created_at, updated_at, "
                "items_json, verdict_note, status) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (email, ticker, now, now, json.dumps(items or []), verdict_note or "", status),
            )
