"""
top100_store.py

Persistence for the Top 100 tab (three tables, same SQLite file/
volume-resolution rule as every other *_store.py on this site - see
watchlist_store.py's own module docstring for why SQLite over a
hand-rolled JSON file):

  top100_pool    - one row per (as_of, ticker): the selected Top 100
                   pool for a given selection run (top100_engine.
                   select_top100_pool()). Multiple as_of snapshots are
                   kept (bounded by prune_old_pool_snapshots()) so the
                   page's own "changes strip" (new this week / dropped,
                   with the reason) can diff the two most recent
                   selections - a single current-only row would have
                   nothing to diff against.
  top100_scores  - one row per (ticker, quarter, model): the AI
                   qualitative score, cached so a nightly run only
                   pays for genuinely unscored entrants (top100_
                   engine.py's own cache/cadence rule). Stores the
                   FULL prompt and raw response text per ticker for
                   reproducibility, per the task's own instruction.
  top100_batch_state - a SINGLETON row (id=1) tracking at most one
                   in-flight Batch API submission at a time - see
                   top100_engine.py's own two-phase submit/poll
                   docstring for why a nightly job never blocks
                   waiting on a batch that can take up to 24h.

Callers never touch SQL directly.
"""

import json
import os
import sqlite3
from datetime import datetime, timezone


def _data_dir():
    return os.environ.get("RAILWAY_VOLUME_MOUNT_PATH") or os.path.dirname(__file__)


DB_PATH = os.path.join(_data_dir(), "stocksdeepdive.db")

# How many distinct as_of pool snapshots to keep - only the two most
# recent are ever read (current + previous, for the changes strip), a
# few extra kept purely as a diagnostic trail.
POOL_SNAPSHOT_RETENTION = 10


def _conn():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS top100_pool (
            as_of TEXT NOT NULL,
            ticker TEXT NOT NULL,
            company_name TEXT,
            universe TEXT,
            value_score REAL,
            mos_pct REAL,
            price REAL,
            intrinsic_value REAL,
            currency TEXT,
            PRIMARY KEY (as_of, ticker)
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS top100_scores (
            ticker TEXT NOT NULL,
            quarter TEXT NOT NULL,
            model TEXT NOT NULL,
            dims_json TEXT NOT NULL,
            not_rated INTEGER NOT NULL,
            summary TEXT,
            prompt TEXT,
            raw_response TEXT,
            scored_at TEXT NOT NULL,
            PRIMARY KEY (ticker, quarter, model)
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS top100_batch_state (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            batch_id TEXT,
            submitted_at TEXT,
            quarter TEXT,
            model TEXT,
            custom_id_map_json TEXT
        )"""
    )
    return conn


# -----------------------------------------------------------------
# Pool (selection).
# -----------------------------------------------------------------

def save_pool(rows, as_of):
    """Upserts one full pool snapshot - `rows`: [{"ticker",
    "company_name", "universe", "value_score", "mos_pct", "price",
    "intrinsic_value", "currency"}, ...], `as_of`: "YYYY-MM-DD". Also
    prunes snapshots beyond POOL_SNAPSHOT_RETENTION in the same call,
    so callers never have to remember to prune separately."""
    with _conn() as conn:
        conn.executemany(
            """INSERT INTO top100_pool
                 (as_of, ticker, company_name, universe, value_score,
                  mos_pct, price, intrinsic_value, currency)
                 VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(as_of, ticker) DO UPDATE SET
                 company_name = excluded.company_name,
                 universe = excluded.universe,
                 value_score = excluded.value_score,
                 mos_pct = excluded.mos_pct,
                 price = excluded.price,
                 intrinsic_value = excluded.intrinsic_value,
                 currency = excluded.currency""",
            [
                (as_of, r["ticker"], r.get("company_name"), r.get("universe"),
                 r.get("value_score"), r.get("mos_pct"), r.get("price"),
                 r.get("intrinsic_value"), r.get("currency"))
                for r in rows
            ],
        )
    _prune_old_pool_snapshots()


def _prune_old_pool_snapshots(keep=POOL_SNAPSHOT_RETENTION):
    with _conn() as conn:
        dates = [r[0] for r in conn.execute(
            "SELECT DISTINCT as_of FROM top100_pool ORDER BY as_of DESC"
        ).fetchall()]
        stale = dates[keep:]
        if stale:
            conn.executemany("DELETE FROM top100_pool WHERE as_of = ?", [(d,) for d in stale])


def _distinct_as_of_dates(limit=2):
    with _conn() as conn:
        return [r[0] for r in conn.execute(
            "SELECT DISTINCT as_of FROM top100_pool ORDER BY as_of DESC LIMIT ?", (limit,)
        ).fetchall()]


def _pool_for_as_of(as_of):
    with _conn() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM top100_pool WHERE as_of = ? ORDER BY value_score DESC",
            (as_of,),
        ).fetchall()
    return [dict(r) for r in rows]


def latest_as_of():
    """The most recent pool selection's "YYYY-MM-DD", or None if no
    selection has ever run."""
    dates = _distinct_as_of_dates(limit=1)
    return dates[0] if dates else None


def current_pool():
    """The most recent pool snapshot - [{"ticker", "company_name",
    "universe", "value_score", "mos_pct", "price", "intrinsic_value",
    "currency"}, ...], Value Score descending. [] if no selection has
    ever run."""
    as_of = latest_as_of()
    return _pool_for_as_of(as_of) if as_of else []


def previous_pool():
    """The SECOND-most-recent pool snapshot, for the changes strip's
    own "new this week / dropped" diff against current_pool(). []
    when fewer than two snapshots exist yet (day one, or the first
    selection after a fresh deploy with no prior history)."""
    dates = _distinct_as_of_dates(limit=2)
    return _pool_for_as_of(dates[1]) if len(dates) > 1 else []


# -----------------------------------------------------------------
# Scores (AI qualitative scoring cache).
# -----------------------------------------------------------------

def save_score(ticker, quarter, model, dims, not_rated, summary, prompt, raw_response):
    """Upserts one ticker's AI score for (quarter, model). `dims`: a
    plain dict {"moat": {"score": int_or_None, "justification": str,
    "source_period": str}, ...} for all ten dimensions - stored as
    JSON, read back exactly as given. `not_rated`: bool, True when
    >=3 dimensions came back null (top100_engine.py's own rule - see
    that module's docstring). `prompt`/`raw_response`: the FULL text
    sent/received, for reproducibility (the task's own instruction) -
    never truncated."""
    with _conn() as conn:
        conn.execute(
            """INSERT INTO top100_scores
                 (ticker, quarter, model, dims_json, not_rated, summary,
                  prompt, raw_response, scored_at)
                 VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(ticker, quarter, model) DO UPDATE SET
                 dims_json = excluded.dims_json,
                 not_rated = excluded.not_rated,
                 summary = excluded.summary,
                 prompt = excluded.prompt,
                 raw_response = excluded.raw_response,
                 scored_at = excluded.scored_at""",
            (ticker, quarter, model, json.dumps(dims), int(bool(not_rated)), summary,
             prompt, raw_response, datetime.now(timezone.utc).isoformat()),
        )


def get_score(ticker, quarter, model):
    """{"ticker","quarter","model","dims","not_rated","summary",
    "prompt","raw_response","scored_at"} for one ticker, or None if
    it hasn't been scored yet for this (quarter, model)."""
    with _conn() as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM top100_scores WHERE ticker = ? AND quarter = ? AND model = ?",
            (ticker, quarter, model),
        ).fetchone()
    if not row:
        return None
    out = dict(row)
    out["dims"] = json.loads(out.pop("dims_json"))
    out["not_rated"] = bool(out["not_rated"])
    return out


def scores_for_quarter_model(quarter, model):
    """{ticker: score_dict, ...} for every ticker already scored this
    (quarter, model) - the nightly job's own "only unscored entrants
    go to the API" check reads this to know what's already cached."""
    with _conn() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM top100_scores WHERE quarter = ? AND model = ?",
            (quarter, model),
        ).fetchall()
    out = {}
    for r in rows:
        d = dict(r)
        d["dims"] = json.loads(d.pop("dims_json"))
        d["not_rated"] = bool(d["not_rated"])
        out[d["ticker"]] = d
    return out


# -----------------------------------------------------------------
# Batch state (at most one in-flight Batch API submission).
# -----------------------------------------------------------------

def save_batch_state(batch_id, quarter, model, custom_id_map):
    with _conn() as conn:
        conn.execute(
            """INSERT INTO top100_batch_state (id, batch_id, submitted_at, quarter, model, custom_id_map_json)
                 VALUES (1, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                 batch_id = excluded.batch_id,
                 submitted_at = excluded.submitted_at,
                 quarter = excluded.quarter,
                 model = excluded.model,
                 custom_id_map_json = excluded.custom_id_map_json""",
            (batch_id, datetime.now(timezone.utc).isoformat(), quarter, model,
             json.dumps(custom_id_map)),
        )


def get_batch_state():
    """{"batch_id","submitted_at","quarter","model","custom_id_map"} for
    the one currently-tracked in-flight batch, or None if none is
    pending."""
    with _conn() as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM top100_batch_state WHERE id = 1"
        ).fetchone()
    if not row or not row["batch_id"]:
        return None
    out = dict(row)
    out["custom_id_map"] = json.loads(out.pop("custom_id_map_json") or "{}")
    return out


def clear_batch_state():
    with _conn() as conn:
        conn.execute("DELETE FROM top100_batch_state WHERE id = 1")
