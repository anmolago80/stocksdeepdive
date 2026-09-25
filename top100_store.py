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
  top100_scores  - one row per (ticker, quarter, model, rubric_
                   version): the AI qualitative score, cached so a
                   nightly run only pays for genuinely unscored
                   entrants (top100_engine.py's own cache/cadence
                   rule). rubric_version (top100_engine.RUBRIC_
                   VERSION) joined the primary key in the v2 amendment
                   (Sep 2026) so a scoring-rubric change invalidates
                   the cache cleanly - see _migrate_scores_schema_v2()
                   below for the rebuild that added it and why a
                   simple ALTER TABLE ADD COLUMN wasn't enough (it
                   changes the PK, which SQLite can't do in place).
                   Stores the FULL prompt and raw response text per
                   ticker for reproducibility, per the task's own
                   instruction.
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


def _table_columns(conn, table):
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]


def _migrate_scores_schema_v2(conn):
    """One-time, idempotent rebuild of top100_scores for the v2 rubric
    (Top 100 amendment v2, Sep 2026) - same rename/rebuild/copy/drop
    shape portfolio_store.py's own _migrate_legacy_schema() already
    established for this codebase's "no migration framework, changing
    a PRIMARY KEY needs a real rebuild" situations (a guarded ALTER
    TABLE ADD COLUMN, used everywhere else in this codebase for a
    purely additive column, can't change a PK).

    WHY the PK has to change: top100_engine.RUBRIC_VERSION is now part
    of the score-cache key (the task's own explicit instruction) - a
    ticker scored under the retired v1 rubric must never be read back
    as if it answered v2's different questions (different dimension
    set entirely: moat/regulatory/balance_sheet_resilience/inflation_
    exposure are gone, competitive_position/regulatory_legal/balance_
    sheet_fixed_charges/management are new). Old PK was (ticker,
    quarter, model); new PK adds rubric_version, so a v1 row and a v2
    row for the same ticker/quarter/model coexist rather than one
    silently overwriting the other.

    Every pre-existing row is copied across untouched, backfilled with
    rubric_version='v1' (the retired, unversioned scheme's own implicit
    name) - never rewritten, never deleted. Two other v2 additions ride
    the same rebuild rather than a separate ALTER TABLE pass, since
    they're new in the same rubric bump: inversion_scenario/inversion_
    severity (the inversion synthesis, task (f)) replace the old
    `summary` column, which v2 no longer asks the model for (see
    top100_engine._response_schema()'s own docstring) - old rows'
    `summary` values are simply not carried forward (that field is
    retired, not migrated) and old rows get inversion_scenario/
    inversion_severity = NULL (v1 never asked the model for them)."""
    cols = _table_columns(conn, "top100_scores")
    if not cols or "rubric_version" in cols:
        return
    conn.execute("ALTER TABLE top100_scores RENAME TO top100_scores_pre_v2")
    conn.execute(
        """CREATE TABLE top100_scores (
            ticker TEXT NOT NULL,
            quarter TEXT NOT NULL,
            model TEXT NOT NULL,
            rubric_version TEXT NOT NULL,
            dims_json TEXT NOT NULL,
            not_rated INTEGER NOT NULL,
            inversion_scenario TEXT,
            inversion_severity INTEGER,
            prompt TEXT,
            raw_response TEXT,
            scored_at TEXT NOT NULL,
            PRIMARY KEY (ticker, quarter, model, rubric_version)
        )"""
    )
    old_cols = _table_columns(conn, "top100_scores_pre_v2")
    old_rows = conn.execute(f"SELECT {', '.join(old_cols)} FROM top100_scores_pre_v2").fetchall()
    for row in old_rows:
        d = dict(zip(old_cols, row))
        conn.execute(
            "INSERT OR IGNORE INTO top100_scores "
            "(ticker, quarter, model, rubric_version, dims_json, not_rated, "
            "inversion_scenario, inversion_severity, prompt, raw_response, scored_at) "
            "VALUES (?, ?, ?, 'v1', ?, ?, NULL, NULL, ?, ?, ?)",
            (d["ticker"], d["quarter"], d["model"], d["dims_json"], d["not_rated"],
             d["prompt"], d["raw_response"], d["scored_at"]),
        )
    conn.execute("DROP TABLE top100_scores_pre_v2")


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
    # v2 amendment: the pooled row's own raw "Psychology" number (see
    # top100_engine.select_top100_pool()'s own comment) - purely
    # additive, no PK change needed, so a guarded ALTER TABLE (this
    # codebase's own standard pattern for exactly this shape of change,
    # e.g. blog_store.py's primary_ticker column) is enough here, unlike
    # top100_scores below.
    try:
        conn.execute("ALTER TABLE top100_pool ADD COLUMN psychology REAL")
    except sqlite3.OperationalError:
        pass
    # Top 100 Commit 5 (25 Sep 2026, owner-reported, industry mock): the
    # pooled row's own "Sector" string, same guarded-ALTER-TABLE pattern
    # as psychology just above (purely additive, no PK change) - so the
    # page can show a display-only industry tag without any new query
    # beyond what select_top100_pool() already reads from the scan row.
    # Never fed into composite_score() - same "display flag only"
    # status as psychology.
    try:
        conn.execute("ALTER TABLE top100_pool ADD COLUMN sector TEXT")
    except sqlite3.OperationalError:
        pass
    # Top 20 Australia guaranteed-twenty (25 Sep 2026, owner-approved
    # mock, "top20_australia_extended_mock.html") - True for an "ASX
    # extension" row (top100_engine.select_top100_pool()'s own
    # next-best-ASX-by-Value-Score top-up, added only when the global
    # pool's own .AX members fall short of 20). Same guarded-ALTER-
    # TABLE, purely-additive-column pattern as psychology/sector above,
    # NOT NULL DEFAULT 0 so every pre-existing row (all genuine pool
    # members) reads unambiguously False rather than NULL. current_
    # pool()/previous_pool() both filter this out unconditionally (see
    # _pool_for_as_of() below) - every existing reader of those two
    # functions is therefore untouched by this column's existence.
    try:
        conn.execute("ALTER TABLE top100_pool ADD COLUMN asx_extension INTEGER NOT NULL DEFAULT 0")
    except sqlite3.OperationalError:
        pass
    conn.execute(
        """CREATE TABLE IF NOT EXISTS top100_scores (
            ticker TEXT NOT NULL,
            quarter TEXT NOT NULL,
            model TEXT NOT NULL,
            rubric_version TEXT NOT NULL,
            dims_json TEXT NOT NULL,
            not_rated INTEGER NOT NULL,
            inversion_scenario TEXT,
            inversion_severity INTEGER,
            prompt TEXT,
            raw_response TEXT,
            scored_at TEXT NOT NULL,
            PRIMARY KEY (ticker, quarter, model, rubric_version)
        )"""
    )
    _migrate_scores_schema_v2(conn)
    # RUBRIC_VERSION v3 (25 Sep 2026, owner-approved mock, "headwind_
    # and_currency_risk_mock.html") - Claude's one-line "why is the
    # market discounting this company right now" answer. Purely
    # additive (nullable, no PK change - unlike the v1->v2 bump, this
    # one needs no _migrate_scores_schema_v3() rebuild at all, since
    # rubric_version already sits in the PK for exactly this reason,
    # see _migrate_scores_schema_v2()'s own docstring). Every existing
    # v1/v2 row simply reads NULL here, which top100_render.py already
    # treats as "no headwind line" - the same status a genuinely-empty
    # ("no clearly identifiable headwind") v3 answer gets.
    try:
        conn.execute("ALTER TABLE top100_scores ADD COLUMN current_headwind TEXT")
    except sqlite3.OperationalError:
        pass
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
    "intrinsic_value", "currency", "psychology", "sector",
    "asx_extension"}, ...], `as_of`: "YYYY-MM-DD". `asx_extension`
    (Top 20 Australia guaranteed-twenty) defaults to False when a row
    doesn't carry it - every caller before this feature existed passes
    plain pool rows and keeps working unchanged. Also prunes snapshots
    beyond POOL_SNAPSHOT_RETENTION in the same call, so callers never
    have to remember to prune separately."""
    with _conn() as conn:
        conn.executemany(
            """INSERT INTO top100_pool
                 (as_of, ticker, company_name, universe, value_score,
                  mos_pct, price, intrinsic_value, currency, psychology, sector,
                  asx_extension)
                 VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(as_of, ticker) DO UPDATE SET
                 company_name = excluded.company_name,
                 universe = excluded.universe,
                 value_score = excluded.value_score,
                 mos_pct = excluded.mos_pct,
                 price = excluded.price,
                 intrinsic_value = excluded.intrinsic_value,
                 currency = excluded.currency,
                 psychology = excluded.psychology,
                 sector = excluded.sector,
                 asx_extension = excluded.asx_extension""",
            [
                (as_of, r["ticker"], r.get("company_name"), r.get("universe"),
                 r.get("value_score"), r.get("mos_pct"), r.get("price"),
                 r.get("intrinsic_value"), r.get("currency"), r.get("psychology"),
                 r.get("sector"), int(bool(r.get("asx_extension"))))
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
    """GLOBAL pool rows only (asx_extension = 0) - current_pool()'s and
    previous_pool()'s shared read, unchanged in contract since before
    the ASX extension existed. current_asx_extension() below is the
    only reader of the OTHER rows."""
    with _conn() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM top100_pool WHERE as_of = ? AND asx_extension = 0 ORDER BY value_score DESC",
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
    "currency", "psychology", "sector"}, ...], Value Score descending.
    [] if no selection has ever run."""
    as_of = latest_as_of()
    return _pool_for_as_of(as_of) if as_of else []


def previous_pool():
    """The SECOND-most-recent pool snapshot, for the changes strip's
    own "new this week / dropped" diff against current_pool(). []
    when fewer than two snapshots exist yet (day one, or the first
    selection after a fresh deploy with no prior history)."""
    dates = _distinct_as_of_dates(limit=2)
    return _pool_for_as_of(dates[1]) if len(dates) > 1 else []


def current_asx_extension():
    """Top 20 Australia guaranteed-twenty (25 Sep 2026, owner-approved
    mock): the ASX EXTENSION rows only (asx_extension = 1) for the
    latest as_of - [{"ticker", ...}, ...] shaped exactly like current_
    pool()'s own rows, Value Score descending. [] once the pool alone
    already has >= top100_engine.TOP20_AU_TARGET Australians (nothing
    to extend with) or no selection has run yet. NEVER returned by
    current_pool()/previous_pool() - this is the only reader of these
    rows; every other Top 100 surface (Full 100, Mixed, USA, the
    homepage teaser, the changes strip) reads current_pool()/previous_
    pool() alone and is therefore unaffected by this function's
    existence."""
    as_of = latest_as_of()
    if not as_of:
        return []
    with _conn() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM top100_pool WHERE as_of = ? AND asx_extension = 1 ORDER BY value_score DESC",
            (as_of,),
        ).fetchall()
    return [dict(r) for r in rows]


# -----------------------------------------------------------------
# Scores (AI qualitative scoring cache).
# -----------------------------------------------------------------

def save_score(ticker, quarter, model, rubric_version, dims, not_rated,
                inversion_scenario, inversion_severity, prompt, raw_response,
                current_headwind=None):
    """Upserts one ticker's AI score for (quarter, model,
    rubric_version) - rubric_version (top100_engine.RUBRIC_VERSION) is
    part of the cache key/PK (see _migrate_scores_schema_v2()'s own
    docstring for why) so a rubric bump never reads an old rubric's
    row back as if it answered the new questions. `dims`: a plain dict
    {"ai_exposure": {"score": int_or_None, "justification": str,
    "source_period": str}, ...} for all ten CURRENT-rubric dimension
    keys - stored as JSON, read back exactly as given. `not_rated`:
    bool, True when >=3 dimensions came back null (top100_engine.py's
    own rule - see that module's docstring). `inversion_scenario`/
    `inversion_severity`: the one-sentence damaging-scenario synthesis
    + 1-5 severity (task's own "inversion synthesis" instruction) -
    both None for a NOT RATED company (top100_engine._parse_response_
    json() enforces this server-side before it ever reaches here).
    `current_headwind` (RUBRIC_VERSION v3, 25 Sep 2026): the one-line
    "why is the market discounting this company right now" answer,
    same None-for-NOT-RATED / None-for-"no clearly identifiable
    headwind" treatment as the inversion fields - defaults to None so
    a caller passing the old (v1/v2-era) argument list still works.
    `prompt`/`raw_response`: the FULL text sent/received, for
    reproducibility (the task's own instruction) - never truncated."""
    with _conn() as conn:
        conn.execute(
            """INSERT INTO top100_scores
                 (ticker, quarter, model, rubric_version, dims_json, not_rated,
                  inversion_scenario, inversion_severity, current_headwind,
                  prompt, raw_response, scored_at)
                 VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(ticker, quarter, model, rubric_version) DO UPDATE SET
                 dims_json = excluded.dims_json,
                 not_rated = excluded.not_rated,
                 inversion_scenario = excluded.inversion_scenario,
                 inversion_severity = excluded.inversion_severity,
                 current_headwind = excluded.current_headwind,
                 prompt = excluded.prompt,
                 raw_response = excluded.raw_response,
                 scored_at = excluded.scored_at""",
            (ticker, quarter, model, rubric_version, json.dumps(dims), int(bool(not_rated)),
             inversion_scenario, inversion_severity, current_headwind,
             prompt, raw_response, datetime.now(timezone.utc).isoformat()),
        )


def get_score(ticker, quarter, model, rubric_version):
    """{"ticker","quarter","model","rubric_version","dims","not_rated",
    "inversion_scenario","inversion_severity","current_headwind",
    "prompt","raw_response","scored_at"} for one ticker, or None if it
    hasn't been scored yet for this exact (quarter, model,
    rubric_version) - a row cached
    under a DIFFERENT rubric_version (e.g. a retired "v1") is never
    returned here, by design."""
    with _conn() as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM top100_scores WHERE ticker = ? AND quarter = ? "
            "AND model = ? AND rubric_version = ?",
            (ticker, quarter, model, rubric_version),
        ).fetchone()
    if not row:
        return None
    out = dict(row)
    out["dims"] = json.loads(out.pop("dims_json"))
    out["not_rated"] = bool(out["not_rated"])
    return out


def scores_for_quarter_model(quarter, model, rubric_version):
    """{ticker: score_dict, ...} for every ticker already scored this
    exact (quarter, model, rubric_version) - the nightly job's own
    "only unscored entrants go to the API" check (top100_engine.
    _unscored_tickers()) reads this to know what's already cached
    UNDER THE CURRENT RUBRIC; a ticker's old-rubric row never
    satisfies this lookup, so a rubric bump makes every pooled ticker
    "unscored" again for one full re-score, per the task's own
    instruction."""
    with _conn() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM top100_scores WHERE quarter = ? AND model = ? AND rubric_version = ?",
            (quarter, model, rubric_version),
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
