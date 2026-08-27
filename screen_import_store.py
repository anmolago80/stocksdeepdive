"""
screen_import_store.py

TradingView screen CSV -> bulk nightly-scan queue.

The owner mass-screens candidates in TradingView (Munger-quality +
cheapness filters), exports the results as a CSV, and uploads it via the
Stock Scanner page's admin-only "Import screen CSV" panel. This module
owns the whole pipeline behind that: parsing TradingView's export format
into Yahoo-style tickers, tracking which of those tickers have been
scanned yet, and handing the pending ones to the nightly job a batch at a
time so a big screen never blows the nightly budget in one go - it just
continues over however many nights it takes, exactly like every other
universe's scan.

WHERE THE FILE LIVES: the same resolution rule as every other persisted
file in this app (see watchlist_store.py / build_compounder_data._cp_data_dir)
- the attached Railway Volume when one exists, falling back to this
directory for local runs. Same DB file as watchlist_store.py/positions_store.py
(stocksdeepdive.db) - SQLite serialises concurrent writers safely, a
hand-rolled JSON read-modify-write cycle does not.

SCHEMA
  screen_imports         - one row per upload: id, name, created_at, the
                            recognised ticker list (tickers_json, for
                            reference/display), and the parse-time
                            skipped/duplicate counts.
  screen_import_tickers   - one row per DISTINCT ticker, keyed globally
                            (not per-import) so a ticker already queued by
                            an earlier import is never re-queued/re-scanned
                            by a later one that happens to include it too.
                            Tracks status (pending/scanned/failed),
                            last_scanned_at, and the scanned result row
                            itself (result_json) so the overnight
                            "imported" scan payload can always be rebuilt
                            from everything scanned so far, not just the
                            most recent batch.

NOTE ON DELETE: deleting an import removes the screen_import_tickers rows
that belong to it. Because a ticker is only ever stored under whichever
import FIRST introduced it (see save_import's dedupe), deleting that
import also drops tracking for that ticker even if a later import's CSV
happened to include it too - an acceptable simplification for a
single-owner admin tool, not a multi-tenant system.
"""

import csv
import io
import json
import os
import re
import sqlite3
from datetime import datetime, timezone


def _data_dir():
    return os.environ.get("RAILWAY_VOLUME_MOUNT_PATH") or os.path.dirname(__file__)


DB_PATH = os.path.join(_data_dir(), "stocksdeepdive.db")

MAX_TICKERS_PER_IMPORT = 500


def _conn():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS screen_imports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            created_at TEXT NOT NULL,
            tickers_json TEXT NOT NULL,
            ticker_count INTEGER NOT NULL,
            skipped_count INTEGER NOT NULL DEFAULT 0,
            duplicate_count INTEGER NOT NULL DEFAULT 0
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS screen_import_tickers (
            ticker TEXT PRIMARY KEY,
            import_id INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            added_at TEXT NOT NULL,
            last_scanned_at TEXT,
            result_json TEXT
        )"""
    )
    return conn


# -----------------------------------
# CSV parsing
# -----------------------------------

_SYMBOL_HEADER_NAMES = {"ticker", "symbol"}
_EXCH_SYMBOL_RE = re.compile(r"^([A-Za-z]+):([A-Za-z0-9.]+)$")
_BARE_SYMBOL_RE = re.compile(r"^[A-Za-z0-9.]+$")

# Exchange prefix -> Yahoo ticker mapper. ASX gets the .AX suffix; the US
# exchanges pass the code through with "." (share classes, e.g. BRK.B)
# swapped for the "-" Yahoo actually uses (BRK-B). Any exchange not listed
# here is deliberately left unmapped (never guessed) - the caller reports
# it as skipped.
_US_EXCHANGES = {"NASDAQ", "NYSE", "AMEX", "CBOE", "OTC"}

# Some TradingView export presets (e.g. a plain "Symbol" column, no
# "EXCHANGE:CODE" prefix at all) drop the exchange entirely - but they
# usually still carry a "Price - Currency" column, which is just as
# reliable a signal: AUD really only means ASX here, USD means one of the
# US exchanges. Used to auto-map a bare code PER ROW (see
# _find_currency_column/parse_tv_csv below) - important because a single
# file can genuinely mix AUD and USD rows (a "USA or AUS" screen), so one
# global guess for the whole file isn't always right.
_CURRENCY_TO_EXCHANGE = {"AUD": "ASX", "USD": "NASDAQ"}
_CURRENCY_HEADER_NAMES = {"price - currency", "currency"}


def _map_symbol(exchange, code):
    exchange = exchange.upper()
    code = code.upper()
    if exchange == "ASX":
        return f"{code}.AX"
    if exchange in _US_EXCHANGES:
        return code.replace(".", "-")
    return None


def _find_symbol_column(header, sample_rows):
    """The symbol column, by header name first (Ticker/Symbol, case
    insensitive), else the first column whose every non-empty sampled
    value matches EXCHANGE:CODE."""
    for i, h in enumerate(header):
        if (h or "").strip().lower() in _SYMBOL_HEADER_NAMES:
            return i
    for i in range(len(header)):
        checked = matched = 0
        for row in sample_rows:
            if i < len(row) and (row[i] or "").strip():
                checked += 1
                if _EXCH_SYMBOL_RE.match(row[i].strip().upper()):
                    matched += 1
        if checked and matched == checked:
            return i
    return None


def _find_currency_column(header):
    """The "Price - Currency" column TradingView exports alongside Price,
    by exact header name (case/spacing insensitive) - or None if this
    export doesn't have one."""
    for i, h in enumerate(header):
        if (h or "").strip().lower() in _CURRENCY_HEADER_NAMES:
            return i
    return None


def parse_tv_csv(text, default_exchange=None):
    """
    Parse a TradingView screener CSV export (as text). Returns:
      {"mapped": [yahoo_ticker, ...],           # deduped, order preserved
       "skipped_unsupported": [(raw, exchange), ...],  # unmapped exchange
       "skipped_unparsed": [raw, ...],           # no usable symbol at all
       "raw_count": int,                         # data rows examined
       "error": str or None}
    `error` set (with the other lists empty) means nothing was imported -
    either the file/column couldn't be read, or the recognised-ticker
    count exceeds MAX_TICKERS_PER_IMPORT (rejected outright, never
    silently truncated - the nightly budget is the real constraint).

    HANDLING A ROW WITH NO "EXCHANGE:CODE" PREFIX AT ALL - just a bare
    code like "OCL" (some TradingView export presets, e.g. a plain
    "Symbol" column, drop the exchange entirely). Left unhandled, a bare
    ASX code with no ".AX" added silently fails every yfinance lookup
    (looks like "delisted", not "wrong ticker"). Two ways this file
    might still say what exchange it means, tried in order:
      1. A "Price - Currency" column (TradingView includes one on most
         presets) - AUD -> ASX, USD -> a US exchange, checked PER ROW,
         since one file can genuinely mix AUD and USD names (a "USA or
         AUS" screen) where a single guess for the whole file would be
         wrong for half of it.
      2. `default_exchange` (one of _US_EXCHANGES, or "ASX", or None) -
         the caller's (the import UI's) fallback for a bare code whose
         row has no usable currency value either. When both this and the
         currency column are unavailable/unrecognised, a bare code is
         assumed to already be Yahoo-formatted and passed through as-is
         (the original, pre-this-parameter behaviour) - never guessed
         silently beyond what the file itself or the caller says.
    """
    empty = {"mapped": [], "skipped_unsupported": [], "skipped_unparsed": [], "raw_count": 0}

    try:
        rows = list(csv.reader(io.StringIO(text)))
    except csv.Error as e:
        return {**empty, "error": f"Couldn't parse this as CSV: {e}"}

    if not rows:
        return {**empty, "error": "The file is empty."}
    header, data_rows = rows[0], [r for r in rows[1:] if any((c or "").strip() for c in r)]
    if not data_rows:
        return {**empty, "error": "No data rows found under the header."}

    col_idx = _find_symbol_column(header, data_rows[:20])
    if col_idx is None:
        return {
            **empty, "raw_count": len(data_rows),
            "error": ("Couldn't find a symbol column - looked for a header named "
                      "'Ticker' or 'Symbol', or a column formatted like "
                      "EXCHANGE:CODE (e.g. ASX:OCL)."),
        }
    ccy_idx = _find_currency_column(header)

    mapped, skipped_unsupported, skipped_unparsed = [], [], []
    seen = set()
    for row in data_rows:
        if col_idx >= len(row):
            continue
        raw = (row[col_idx] or "").strip()
        if not raw:
            continue
        m = _EXCH_SYMBOL_RE.match(raw)
        if m:
            exch, code = m.group(1), m.group(2)
            yahoo = _map_symbol(exch, code)
            if yahoo is None:
                skipped_unsupported.append((raw, exch.upper()))
                continue
        elif _BARE_SYMBOL_RE.match(raw):
            # No EXCHANGE: prefix - figure out the exchange for THIS row:
            # this row's own currency value first (most reliable, and
            # correct even when a file mixes AUD/USD rows), else the
            # caller's file-wide default_exchange, else leave as-is
            # (assumed already Yahoo-formatted - unchanged pre-currency-
            # detection behaviour).
            row_ccy = (row[ccy_idx] or "").strip().upper() if (
                ccy_idx is not None and ccy_idx < len(row)
            ) else ""
            row_exchange = _CURRENCY_TO_EXCHANGE.get(row_ccy) or default_exchange
            if row_exchange and not raw.upper().endswith(".AX"):
                yahoo = _map_symbol(row_exchange, raw)
                if yahoo is None:
                    skipped_unsupported.append((raw, row_exchange.upper()))
                    continue
            else:
                yahoo = raw.upper()
        else:
            skipped_unparsed.append(raw)
            continue
        if yahoo not in seen:
            seen.add(yahoo)
            mapped.append(yahoo)

    if len(mapped) > MAX_TICKERS_PER_IMPORT:
        return {
            **empty, "raw_count": len(data_rows),
            "error": (f"{len(mapped)} recognised tickers exceeds the "
                      f"{MAX_TICKERS_PER_IMPORT}-per-import cap (the nightly scan "
                      "budget is the constraint) - split this into smaller files."),
        }

    return {
        "mapped": mapped, "skipped_unsupported": skipped_unsupported,
        "skipped_unparsed": skipped_unparsed, "raw_count": len(data_rows), "error": None,
    }


# -----------------------------------
# Store API
# -----------------------------------

def all_imported_tickers():
    """Every ticker tracked across every import, any status - used for a
    live "Run Scan" over the whole imported cohort and for dedupe checks."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT ticker FROM screen_import_tickers ORDER BY added_at"
        ).fetchall()
    return [r[0] for r in rows]


def save_import(name, tickers, skipped_count=0):
    """
    Record a new import and queue every ticker in `tickers` that isn't
    ALREADY tracked globally (across every other import) as pending.
    `tickers` should already be the parsed/mapped/deduped-within-file list
    from parse_tv_csv(); skipped_count is purely informational (stored for
    display on the imports table).

    Returns {"id", "name", "added": [...], "duplicate": [...]} - "added"
    is what got newly queued under this import; "duplicate" is what was
    recognised but already belongs to an earlier import, so left alone
    there rather than re-queued here.
    """
    name = (name or "").strip() or f"Import {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
    tickers = [t.upper() for t in (tickers or [])]
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as conn:
        existing = {r[0] for r in conn.execute("SELECT ticker FROM screen_import_tickers").fetchall()}
        added = [t for t in tickers if t not in existing]
        duplicate = [t for t in tickers if t in existing]
        cur = conn.execute(
            "INSERT INTO screen_imports "
            "(name, created_at, tickers_json, ticker_count, skipped_count, duplicate_count) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (name, now, json.dumps(tickers), len(tickers), int(skipped_count), len(duplicate)),
        )
        import_id = cur.lastrowid
        conn.executemany(
            "INSERT INTO screen_import_tickers (ticker, import_id, status, added_at) "
            "VALUES (?, ?, 'pending', ?)",
            [(t, import_id, now) for t in added],
        )
    return {"id": import_id, "name": name, "added": added, "duplicate": duplicate}


def list_imports():
    """[{"id","name","created_at","ticker_count","skipped_count",
         "duplicate_count","scanned_count","pending_count","failed_count"}],
    newest first - the admin panel's imports table."""
    with _conn() as conn:
        imports = conn.execute(
            "SELECT id, name, created_at, ticker_count, skipped_count, duplicate_count "
            "FROM screen_imports ORDER BY created_at DESC"
        ).fetchall()
        out = []
        for (iid, name, created_at, ticker_count, skipped_count, duplicate_count) in imports:
            by_status = dict(conn.execute(
                "SELECT status, COUNT(*) FROM screen_import_tickers "
                "WHERE import_id = ? GROUP BY status",
                (iid,),
            ).fetchall())
            out.append({
                "id": iid, "name": name, "created_at": created_at,
                "ticker_count": ticker_count, "skipped_count": skipped_count,
                "duplicate_count": duplicate_count,
                "scanned_count": by_status.get("scanned", 0),
                "pending_count": by_status.get("pending", 0),
                "failed_count": by_status.get("failed", 0),
            })
    return out


def get_pending(limit=None):
    """Pending tickers across ALL imports, oldest-queued first (a fair
    queue across imports) - what the nightly job and "Scan next N now"
    both pull from."""
    q = "SELECT ticker FROM screen_import_tickers WHERE status = 'pending' ORDER BY added_at ASC"
    if limit:
        q += f" LIMIT {int(limit)}"
    with _conn() as conn:
        rows = conn.execute(q).fetchall()
    return [r[0] for r in rows]


def get_pending_for_import(import_id, limit=None):
    """Pending tickers belonging to ONE import, oldest-queued first - what
    the admin panel's per-import "Scan next N now" button pulls from (as
    opposed to get_pending(), which pools every import together for the
    nightly job's fair queue)."""
    q = ("SELECT ticker FROM screen_import_tickers "
         "WHERE import_id = ? AND status = 'pending' ORDER BY added_at ASC")
    if limit:
        q += f" LIMIT {int(limit)}"
    with _conn() as conn:
        rows = conn.execute(q, (import_id,)).fetchall()
    return [r[0] for r in rows]


def scanned_rows_for_import(import_id):
    """Every successfully-scanned result row belonging to ONE import, most
    recently scanned first - what the admin panel shows per-import so
    reviewing "Import A" never surfaces a ticker that actually came from
    a different import (see all_scanned_rows(), which pools every import
    together for the nightly job's fair-queue scanning - display stays
    per-import, only the scan queue itself is shared)."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT result_json FROM screen_import_tickers "
            "WHERE import_id = ? AND status = 'scanned' AND result_json IS NOT NULL "
            "ORDER BY last_scanned_at DESC",
            (import_id,),
        ).fetchall()
    out = []
    for (rj,) in rows:
        try:
            out.append(json.loads(rj))
        except (TypeError, ValueError):
            continue
    return out


def mark_scanned(ticker, ok, row=None):
    """Record the outcome of scanning one ticker. `row` (the same dict
    shape nightly_scan.analyze_ticker_lite returns) is stored so
    all_scanned_rows() can rebuild the full ranked "imported" cohort
    without re-scanning anything."""
    now = datetime.now(timezone.utc).isoformat()
    status = "scanned" if ok else "failed"
    result_json = json.dumps(row) if (ok and row is not None) else None
    with _conn() as conn:
        conn.execute(
            "UPDATE screen_import_tickers SET status = ?, last_scanned_at = ?, "
            "result_json = COALESCE(?, result_json) WHERE ticker = ?",
            (status, now, result_json, ticker),
        )


def all_scanned_rows():
    """Every successfully-scanned ticker's stored result row, across every
    import - the nightly "imported" universe job and the admin "Scan next
    N now" button both rebuild scan_store's imported.json from this after
    each batch, so it always reflects everything scanned so far."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT result_json FROM screen_import_tickers "
            "WHERE status = 'scanned' AND result_json IS NOT NULL"
        ).fetchall()
    out = []
    for (rj,) in rows:
        try:
            out.append(json.loads(rj))
        except (TypeError, ValueError):
            continue
    return out


def delete_import(import_id):
    with _conn() as conn:
        conn.execute("DELETE FROM screen_import_tickers WHERE import_id = ?", (import_id,))
        conn.execute("DELETE FROM screen_imports WHERE id = ?", (import_id,))
