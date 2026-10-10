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
from datetime import datetime, timedelta, timezone


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
    # Dividend yield display (26 Sep 2026, owner-approved mock): the
    # pooled row's own "Dividend Yield %" number, same guarded-ALTER-
    # TABLE pattern as psychology/sector above - purely additive,
    # display-only, never fed into composite_score(). Existing rows
    # simply read NULL until the next nightly pool selection populates
    # it, same self-heals-overnight behaviour sector had on its own
    # introduction.
    try:
        conn.execute("ALTER TABLE top100_pool ADD COLUMN dividend_yield_pct REAL")
    except sqlite3.OperationalError:
        pass
    # Results-driven Top 100 refresh (27 Sep 2026, owner-directed): the
    # pooled row's own most recently reported period (nightly_scan.py's
    # "Most Recent Quarter", an ISO "YYYY-MM-DD" or None), same guarded-
    # ALTER-TABLE/purely-additive pattern as psychology/sector/dividend_
    # yield_pct above - but unlike those three, this one is NOT display-
    # only: top100_engine._unscored_tickers() reads it to decide whether
    # a pooled company needs a fresh AI score (see that function's own
    # docstring for the a/b/c rule). Still never fed into composite_
    # score() or any sort/selection - it decides WHETHER to re-score, not
    # the score itself.
    try:
        conn.execute("ALTER TABLE top100_pool ADD COLUMN most_recent_quarter TEXT")
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
    # Top 100 Commit 3 (27 Sep 2026, owner-reported): the OTHER ticker
    # in a share-class pair (top100_engine.SHARE_CLASS_PAIRS) this row's
    # own ticker won on average traded volume, e.g. "GOOGL" on GOOG's
    # own row - display only, NULL for every row that isn't the winner
    # of a real pair. Same guarded-ALTER-TABLE pattern as every other
    # purely-additive column above.
    try:
        conn.execute("ALTER TABLE top100_pool ADD COLUMN also_trades_as TEXT")
    except sqlite3.OperationalError:
        pass
    # Top 100 selection freshness fix (30 Sep 2026, owner-directed): the
    # winning candidate row's own scan file generated_at (ISO string) -
    # what actually decided this ticker won its slot (freshest wins, not
    # highest Long Score - see top100_engine.select_top100_pool()'s own
    # docstring). source_universe_generated_at is kept alongside it,
    # identical today (there is only ever one source per selection) -
    # a separate column so a future selection rule that draws a row's
    # display fields and its generated_at from two different places has
    # somewhere to record that divergence without another schema change.
    # stale_valuation is 1 when this ticker's freshest available
    # candidate is still older than top100_engine.POOL_MAX_FULL_SCAN_
    # AGE_DAYS (no fresher one existed) - the row is never dropped for
    # this alone, only flagged. value_score_source_row is the winning
    # row's own STORED Long Score, before the discovery_measured=False
    # recompute that now fills value_score - audit only, never read by
    # any sort/selection/composite_score(). pool_selection_rule tags
    # which selection RULE chose this row (top100_engine.
    # POOL_SELECTION_RULE) - top100_render.py's changes strip reads it
    # to detect a rule change between current_pool() and previous_
    # pool() and suppress the New/Dropped/Awaiting diff for the one
    # night that comparison would otherwise be artificial. All five are
    # purely additive - same guarded-ALTER-TABLE pattern as every column
    # above.
    for _col, _decl in (
        ("generated_at", "TEXT"),
        ("source_universe_generated_at", "TEXT"),
        ("stale_valuation", "INTEGER NOT NULL DEFAULT 0"),
        ("value_score_source_row", "REAL"),
        ("pool_selection_rule", "TEXT"),
    ):
        try:
            conn.execute(f"ALTER TABLE top100_pool ADD COLUMN {_col} {_decl}")
        except sqlite3.OperationalError:
            pass
    # COMMIT 3 of instruction_top200_unrated_and_blank_replies_combined.md
    # (5 Oct 2026, Director-directed, switch TOP200_BACKFILL_LIVE, OFF
    # by default) - True for a candidate top100_engine._apply_backfill()
    # set aside (UNRATED_MODEL/UNRATED_FAILED, so it never counts toward
    # the 200 places) but still persisted so the public "Not rated"
    # section and the Admin preview can read it back without
    # recomputing selection. Same guarded-ALTER-TABLE, purely-additive-
    # column, NOT NULL DEFAULT 0 pattern as asx_extension above -
    # current_pool()/previous_pool() filter this out unconditionally
    # (see _pool_for_as_of() below), so with the switch OFF (no row is
    # ever saved with this column True) every existing reader is
    # completely untouched. backfill_set_aside_status ("unrated_model"/
    # "unrated_failed") is display-only, read by the public page to
    # decide which of the two "Not rated" groups a row belongs to.
    try:
        conn.execute("ALTER TABLE top100_pool ADD COLUMN backfill_set_aside INTEGER NOT NULL DEFAULT 0")
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute("ALTER TABLE top100_pool ADD COLUMN backfill_set_aside_status TEXT")
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
    # RUBRIC_VERSION v4 (26 Sep 2026, owner-approved mock, "top100_v4_
    # market_structure_onefoot_mock.html") - two more questions, same
    # purely-additive/nullable pattern as current_headwind just above
    # (no _migrate_scores_schema_v4() rebuild needed, same reason).
    # Every existing v1-v3 row simply reads NULL here, which
    # top100_render.py's competitive-landscape box already treats as
    # "no line for this verdict" - the same status a genuinely-declined
    # v4 answer gets.
    for _col in ("market_structure", "market_structure_comment",
                 "one_foot_hurdle", "one_foot_comment"):
        try:
            conn.execute(f"ALTER TABLE top100_scores ADD COLUMN {_col} TEXT")
        except sqlite3.OperationalError:
            pass
    # RUBRIC_VERSION v5 (30 Sep 2026, owner-approved mock, "mock_top100_
    # v5_munger_line.html") - two more questions, same purely-additive/
    # nullable pattern as market_structure/one_foot_hurdle just above
    # (no _migrate_scores_schema_v5() rebuild needed, same reason).
    # Every existing v1-v4 row simply reads NULL here, which top100_
    # render.py's competitive-landscape box already treats as "no line
    # for this verdict" - the same status a genuinely-declined v5
    # answer gets.
    for _col in ("munger_quality", "munger_comment", "big_wave", "big_wave_comment"):
        try:
            conn.execute(f"ALTER TABLE top100_scores ADD COLUMN {_col} TEXT")
        except sqlite3.OperationalError:
            pass
    # Results-driven Top 100 refresh (27 Sep 2026, owner-directed) - see
    # this module's own module docstring section below for the full
    # redefinition. Same purely-additive/nullable pattern as every prior
    # rubric bump above (no table rebuild - the `quarter` column's PK
    # role is unchanged, only what STRING gets written into it for new
    # rows changes). This column stores the `most_recent_quarter` the
    # company had AT THE TIME it was scored, read back without having to
    # parse it out of the `quarter`/score-key string - every pre-existing
    # row (scored under the old calendar-quarter regime) simply reads
    # NULL here, which top100_engine._unscored_tickers() already treats
    # as "unknown, never itself a trigger" (see that function's own
    # docstring).
    try:
        conn.execute("ALTER TABLE top100_scores ADD COLUMN most_recent_quarter TEXT")
    except sqlite3.OperationalError:
        pass
    # Degenerate-response guard (3 Oct 2026, owner-directed, live
    # evidence: msgbatch_01Jbz1uEmi1zesnrEbVHtchE's t100-3/t100-17 -
    # sentinel-filled templates, not real judgements, saved as NOT
    # RATED). Same purely-additive/nullable pattern as every prior
    # column above - 0/NULL on every pre-existing row (never a
    # degenerate-accepted row before this existed), 1 only on a row
    # top100_engine._save_degenerate_as_not_rated() wrote after a
    # SECOND consecutive degenerate attempt - see that function's own
    # docstring. top100_render.py's shelf reads it to show a
    # "degenerate x2" caption distinct from an ordinary NOT RATED row.
    try:
        conn.execute("ALTER TABLE top100_scores ADD COLUMN degenerate_accepted INTEGER")
    except sqlite3.OperationalError:
        pass
    # Stored score viewer (3 Oct 2026, owner-directed): records HOW this
    # row's ticker was resolved from the packed response - "ticker" (the
    # model echoed a usable ticker string) or "position" (blank-ticker
    # positional fallback, see _parse_response_json()'s own docstring).
    # NULL for every row saved before this column existed, and for any
    # row saved through a path that doesn't pass it (e.g. the Admin
    # "Recompute this ticker now" panel, which doesn't go through the
    # batch-response parser at all) - the viewer shows "not recorded"
    # rather than guessing.
    try:
        conn.execute("ALTER TABLE top100_scores ADD COLUMN matched_by TEXT")
    except sqlite3.OperationalError:
        pass
    # COMMIT 4 of instruction_top200_unrated_and_blank_replies_combined.md
    # (5 Oct 2026, Director-directed, switch TOP200_SCHEMA_MODE, unset
    # by default) - "legacy" or "ticker_first", the mode top100_engine.
    # top200_schema_mode() read AT SUBMISSION time for the request this
    # row's answer came from. NULL for every pre-existing row and for
    # any caller that doesn't pass it - the display layer (Admin's
    # schema_mode breakdown table) treats NULL/None the same as
    # "legacy", the task's own explicit rule.
    try:
        conn.execute("ALTER TABLE top100_scores ADD COLUMN schema_mode TEXT")
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
    # Audit fix B1 / Fable finding T4 (10 Oct 2026): batches.create() is
    # now called with max_retries=0 (see top100_engine.submit_nightly_
    # batch()'s own comment) - a non-idempotent create MUST NOT be auto-
    # retried by the SDK, since a retry after a response-delivery
    # failure (the request reached the server and a batch WAS created,
    # but the client never saw the confirmation) would submit a real
    # second batch. This one-row marker is written BEFORE the create()
    # call and only cleared once the call's outcome is known - either
    # a normal success/failure, or, on the NEXT call, after reconciling
    # against batches.list() to check whether that earlier attempt
    # actually went through server-side.
    conn.execute(
        """CREATE TABLE IF NOT EXISTS top100_submitting_marker (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            attempted_at TEXT,
            quarter TEXT,
            model TEXT,
            custom_id_map_json TEXT
        )"""
    )
    # Audit fixes, Commit 1 (30 Sep 2026, owner-directed, "close the no-
    # resubmit-loop door") - see top100_engine.py's own TOP100_FAILURE_*
    # constants docstring for the full root cause. One row per (ticker,
    # model, rubric_version) - rubric_version is part of the PK for the
    # same reason it's part of top100_scores' own PK: a rubric bump is a
    # brand-new question set, so a failure recorded under a retired
    # rubric must never block scoring under the new one.
    conn.execute(
        """CREATE TABLE IF NOT EXISTS top100_score_failures (
            ticker TEXT NOT NULL,
            model TEXT NOT NULL,
            rubric_version TEXT NOT NULL,
            failed_at TEXT NOT NULL,
            reason TEXT,
            attempts INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (ticker, model, rubric_version)
        )"""
    )
    # COMMIT 2 of instruction_top200_unrated_and_blank_replies_combined.md
    # (5 Oct 2026, Director-directed, "a failed company is never stuck
    # for good") - a failure row has no score row to read most_recent_
    # quarter from, so this records it separately at failure time (the
    # pooled row's own value, passed in by top100_engine.poll_and_
    # ingest_batch() at every record_score_failure() call site - see
    # that function's own failure_reasons list) purely so top100_
    # engine._failure_reentry_reason() can apply the SAME new_results
    # trigger NOT RATED score rows already get. NULL for every pre-
    # existing failure row and for any caller that doesn't pass it -
    # _failure_reentry_reason() then falls back to the age trigger
    # alone, exactly as a score row with a missing most_recent_quarter
    # would.
    try:
        conn.execute("ALTER TABLE top100_score_failures ADD COLUMN most_recent_quarter TEXT")
    except sqlite3.OperationalError:
        pass
    # COMMIT 4 - same column, same "legacy unless recorded" rule, on
    # the failures table too (a request_blank/degenerate_response/etc.
    # failure also comes from a request this schema_mode governed).
    try:
        conn.execute("ALTER TABLE top100_score_failures ADD COLUMN schema_mode TEXT")
    except sqlite3.OperationalError:
        pass
    # Same commit - the hard daily spend cap's own persistence, keyed by
    # UTC date so it resets automatically at day rollover with no extra
    # bookkeeping (no row for a date means 0 batches/0 entrants so far).
    conn.execute(
        """CREATE TABLE IF NOT EXISTS top100_daily_submissions (
            utc_date TEXT PRIMARY KEY,
            batches INTEGER NOT NULL DEFAULT 0,
            entrants INTEGER NOT NULL DEFAULT 0
        )"""
    )
    # Honest cost accounting (1 Oct 2026, owner-directed) - one row per
    # ingested batch, so the Admin Dashboard's own "last 7 days" cost
    # panel has real, persisted figures to sum rather than re-deriving
    # anything from the scheduler's own log lines. See top100_engine.
    # poll_and_ingest_batch()'s own docstring for what writes this.
    conn.execute(
        """CREATE TABLE IF NOT EXISTS top100_ingest_log (
            batch_id TEXT PRIMARY KEY,
            ingested_at TEXT NOT NULL,
            scored INTEGER NOT NULL DEFAULT 0,
            failed INTEGER NOT NULL DEFAULT 0,
            input_tokens INTEGER NOT NULL DEFAULT 0,
            cache_creation_tokens INTEGER NOT NULL DEFAULT 0,
            cache_read_tokens INTEGER NOT NULL DEFAULT 0,
            output_tokens INTEGER NOT NULL DEFAULT 0,
            cost_usd REAL NOT NULL DEFAULT 0
        )"""
    )
    # Batch inspector (3 Oct 2026, owner-directed, resubmission-loop
    # investigation): top100_batch_state's own custom_id_map_json is
    # DELETED by clear_batch_state() right after a batch ingests (see
    # poll_and_ingest_batch()'s own call), so once a batch is ingested
    # there is no longer any record of which tickers each custom_id
    # (packed request) was actually EXPECTED to carry - only the Admin
    # Dashboard's new Batch inspector panel needs this, to tell "this
    # request came back with fewer items than it was given" from "this
    # request's own expected count is simply unknown". Purely additive,
    # no PK change, same guarded-ALTER-TABLE pattern as every other
    # column added to an existing table in this module. NULL for every
    # batch ingested before this column existed - the inspector reports
    # "not available" for those rather than guessing.
    try:
        conn.execute("ALTER TABLE top100_ingest_log ADD COLUMN custom_id_map_json TEXT")
    except sqlite3.OperationalError:
        pass
    # Newcomer persistence filter (1 Oct 2026, owner decision, v6 cost
    # task) - one row per ticker ever seen in a pool selection, tracking
    # how many CONSECUTIVE nightly selections it has just appeared in.
    # top100_engine._true_newcomer_gated() reads this to decide whether
    # a never-scored newcomer may be submitted yet; update_pool_presence()
    # (called from select_top100_pool(), every night) is the only writer
    # besides the one-off v6 seeding helper below. No PK change needed
    # over any prior table here, so this is a plain CREATE TABLE IF NOT
    # EXISTS, same convention as top100_ingest_log just above.
    conn.execute(
        """CREATE TABLE IF NOT EXISTS pool_presence (
            ticker TEXT PRIMARY KEY,
            first_seen_utc_date TEXT NOT NULL,
            consecutive_nights INTEGER NOT NULL DEFAULT 1,
            last_seen_utc_date TEXT NOT NULL
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
    "dividend_yield_pct", "most_recent_quarter", "also_trades_as",
    "asx_extension", "generated_at", "source_universe_generated_at",
    "stale_valuation", "value_score_source_row", "pool_selection_rule"},
    ...], `as_of`: "YYYY-MM-DD". `asx_extension`
    (Top 20 Australia guaranteed-twenty) defaults to False when a row
    doesn't carry it - every caller before this feature existed passes
    plain pool rows and keeps working unchanged; `most_recent_quarter`
    (results-driven Top 100 refresh, 27 Sep 2026) and `also_trades_as`
    (Top 100 Commit 3, 27 Sep 2026 - the other ticker in a share-class
    pair this row won on average traded volume) likewise default to
    None for any caller that doesn't carry them. The five freshness
    columns (selection freshness fix, 30 Sep 2026) default to None/0/
    None the same way - a caller that predates this fix (there is none
    left in this codebase, but a test fixture might reasonably omit
    them) still inserts cleanly. Also prunes snapshots beyond POOL_
    SNAPSHOT_RETENTION in the same call, so callers never have to
    remember to prune separately."""
    with _conn() as conn:
        conn.executemany(
            """INSERT INTO top100_pool
                 (as_of, ticker, company_name, universe, value_score,
                  mos_pct, price, intrinsic_value, currency, psychology, sector,
                  dividend_yield_pct, most_recent_quarter, also_trades_as, asx_extension,
                  generated_at, source_universe_generated_at, stale_valuation,
                  value_score_source_row, pool_selection_rule, backfill_set_aside,
                  backfill_set_aside_status)
                 VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                 dividend_yield_pct = excluded.dividend_yield_pct,
                 most_recent_quarter = excluded.most_recent_quarter,
                 also_trades_as = excluded.also_trades_as,
                 asx_extension = excluded.asx_extension,
                 generated_at = excluded.generated_at,
                 source_universe_generated_at = excluded.source_universe_generated_at,
                 stale_valuation = excluded.stale_valuation,
                 value_score_source_row = excluded.value_score_source_row,
                 pool_selection_rule = excluded.pool_selection_rule,
                 backfill_set_aside = excluded.backfill_set_aside,
                 backfill_set_aside_status = excluded.backfill_set_aside_status""",
            [
                (as_of, r["ticker"], r.get("company_name"), r.get("universe"),
                 r.get("value_score"), r.get("mos_pct"), r.get("price"),
                 r.get("intrinsic_value"), r.get("currency"), r.get("psychology"),
                 r.get("sector"), r.get("dividend_yield_pct"), r.get("most_recent_quarter"),
                 r.get("also_trades_as"), int(bool(r.get("asx_extension"))),
                 r.get("generated_at"), r.get("source_universe_generated_at"),
                 int(bool(r.get("stale_valuation"))), r.get("value_score_source_row"),
                 r.get("pool_selection_rule"), int(bool(r.get("backfill_set_aside"))),
                 r.get("backfill_set_aside_status"))
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
    """GLOBAL pool rows only (asx_extension = 0, backfill_set_aside = 0)
    - current_pool()'s and previous_pool()'s shared read, unchanged in
    contract since before the ASX extension existed. current_asx_
    extension()/current_backfill_set_aside() below are the only
    readers of the OTHER rows. The backfill_set_aside filter (COMMIT 3
    of instruction_top200_unrated_and_blank_replies_combined.md, 5 Oct
    2026) is a no-op with the TOP200_BACKFILL_LIVE switch OFF - no row
    is ever saved with that column True in that state, so every caller
    of current_pool()/previous_pool() is byte-identical to before this
    commit."""
    with _conn() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM top100_pool WHERE as_of = ? AND asx_extension = 0 "
            "AND backfill_set_aside = 0 ORDER BY value_score DESC",
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
    "currency", "psychology", "sector", "dividend_yield_pct"}, ...],
    Value Score descending. [] if no selection has ever run."""
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


def current_backfill_set_aside():
    """COMMIT 3 of instruction_top200_unrated_and_blank_replies_
    combined.md (5 Oct 2026, Director-directed, switch TOP200_
    BACKFILL_LIVE) - the SET-ASIDE rows only (backfill_set_aside = 1)
    for the latest as_of, Value Score descending. [] with the switch
    OFF (no row is ever saved with this column True in that state), or
    once a selection has run with the switch ON but nothing was set
    aside. NEVER returned by current_pool()/previous_pool()/current_
    asx_extension() - this is the only reader of these rows. Each row
    carries its own "backfill_set_aside_status" ("unrated_model" or
    "unrated_failed") so the public page's "Not rated" section can
    sort a set-aside company into the right one of its two groups
    without recomputing company_status()."""
    as_of = latest_as_of()
    if not as_of:
        return []
    with _conn() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM top100_pool WHERE as_of = ? AND backfill_set_aside = 1 ORDER BY value_score DESC",
            (as_of,),
        ).fetchall()
    return [dict(r) for r in rows]


# -----------------------------------------------------------------
# Scores (AI qualitative scoring cache).
# -----------------------------------------------------------------

def save_score(ticker, quarter, model, rubric_version, dims, not_rated,
                inversion_scenario, inversion_severity, prompt, raw_response,
                current_headwind=None, market_structure=None, market_structure_comment=None,
                one_foot_hurdle=None, one_foot_comment=None, most_recent_quarter=None,
                munger_quality=None, munger_comment=None, big_wave=None, big_wave_comment=None,
                degenerate_accepted=False, matched_by=None, schema_mode=None):
    """Upserts one ticker's AI score for (quarter, model,
    rubric_version) - rubric_version (top100_engine.RUBRIC_VERSION) is
    part of the cache key/PK (see _migrate_scores_schema_v2()'s own
    docstring for why) so a rubric bump never reads an old rubric's
    row back as if it answered the new questions.

    `quarter` (results-driven Top 100 refresh, 27 Sep 2026): despite the
    name, this is no longer a calendar-quarter label for a NEW row - it's
    the SCORE KEY top100_engine computes per entrant, either "RP<YYYY-MM-
    DD>" (the most_recent_quarter the company had when scored) or
    "D<YYYY-MM-DD>" (the scoring date, when that's unknown or a same-
    period age-triggered rescore would otherwise collide with the
    existing row - see top100_engine._unscored_tickers()/_score_key_for_
    entrant()'s own docstrings). Existing "2026Q3"-style rows from before
    this change keep that literal value untouched - this column's PK role
    and this function's own upsert behaviour are otherwise unchanged, so
    old and new-style keys coexist in the same table with no migration.

    `dims`: a plain dict {"ai_exposure": {"score": int_or_None,
    "justification": str, "source_period": str}, ...} for all ten
    CURRENT-rubric dimension keys - stored as JSON, read back exactly as
    given. `not_rated`: bool, True when >=3 dimensions came back null
    (top100_engine.py's own rule - see that module's docstring).
    `inversion_scenario`/`inversion_severity`: the one-sentence damaging-
    scenario synthesis + 1-5 severity (task's own "inversion synthesis"
    instruction) - both None for a NOT RATED company (top100_engine.
    _parse_response_json() enforces this server-side before it ever
    reaches here).
    `current_headwind` (RUBRIC_VERSION v3, 25 Sep 2026): the one-line
    "why is the market discounting this company right now" answer,
    same None-for-NOT-RATED / None-for-"no clearly identifiable
    headwind" treatment as the inversion fields - defaults to None so
    a caller passing the old (v1/v2-era) argument list still works.
    `market_structure`/`market_structure_comment`/`one_foot_hurdle`/
    `one_foot_comment` (RUBRIC_VERSION v4, 26 Sep 2026): the
    competitive-structure label + comment and the one-foot-hurdle
    verdict + comment - top100_engine._parse_response_json() has
    already validated each label against its own small fixed
    vocabulary and nulled the matching comment whenever its label is
    None, so this function stores exactly what it's given, no further
    validation here. All four default to None so a caller passing the
    old (v1-v3-era) argument list still works.
    `most_recent_quarter` (results-driven Top 100 refresh, 27 Sep 2026):
    the pool row's own most_recent_quarter AT THE TIME this score was
    taken - stored alongside the score key (not just baked into the key
    string) so latest_scores_for_model()/_unscored_tickers() can read it
    back directly. Defaults to None so a caller passing the old
    argument list still works, and every pre-existing row simply reads
    NULL here (see this column's own ALTER TABLE comment above).
    `munger_quality`/`munger_comment`/`big_wave`/`big_wave_comment`
    (RUBRIC_VERSION v5, 30 Sep 2026): the Munger-quality verdict +
    comment and the big-wave-to-ride verdict + comment - top100_engine.
    _parse_response_json() has already validated each label against its
    own small fixed vocabulary and nulled the matching comment whenever
    its label is None, so this function stores exactly what it's given,
    no further validation here. All four default to None so a caller
    passing the old (v1-v4-era) argument list still works.
    `prompt`/`raw_response`: the FULL text sent/received, for
    reproducibility (the task's own instruction) - never truncated.
    `matched_by` (stored score viewer, 3 Oct 2026; "whole_response"
    added by Commit 1, 4 Oct 2026): "ticker" or "position", whichever
    way top100_engine._parse_response_json() resolved this ticker from
    the packed response, or "whole_response" when the entire packed
    response was degenerate and every one of its entrants - not just
    whichever one an explicit/positional ticker happened to land on -
    was recorded under the same reason (see _whole_response_is_
    degenerate()'s own docstring) - None for a caller that doesn't pass
    it (e.g. a pre-existing row, or a save made outside the batch-
    ingest path).
    `schema_mode` (COMMIT 4 of instruction_top200_unrated_and_blank_
    replies_combined.md, 5 Oct 2026): "legacy" or "ticker_first" -
    top100_engine.top200_schema_mode()'s own reading AT THE TIME this
    request was actually submitted (never re-read at ingest time - see
    poll_and_ingest_batch()'s own docstring for why). None for a caller
    that doesn't pass it; every pre-existing row reads NULL here, and
    the display layer treats NULL/None the same as "legacy" (the task's
    own "existing rows are not rewritten and count as legacy" rule)."""
    with _conn() as conn:
        conn.execute(
            """INSERT INTO top100_scores
                 (ticker, quarter, model, rubric_version, dims_json, not_rated,
                  inversion_scenario, inversion_severity, current_headwind,
                  market_structure, market_structure_comment,
                  one_foot_hurdle, one_foot_comment, most_recent_quarter,
                  munger_quality, munger_comment, big_wave, big_wave_comment,
                  degenerate_accepted, matched_by, schema_mode, prompt, raw_response, scored_at)
                 VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(ticker, quarter, model, rubric_version) DO UPDATE SET
                 dims_json = excluded.dims_json,
                 not_rated = excluded.not_rated,
                 inversion_scenario = excluded.inversion_scenario,
                 inversion_severity = excluded.inversion_severity,
                 current_headwind = excluded.current_headwind,
                 most_recent_quarter = excluded.most_recent_quarter,
                 market_structure = excluded.market_structure,
                 market_structure_comment = excluded.market_structure_comment,
                 one_foot_hurdle = excluded.one_foot_hurdle,
                 one_foot_comment = excluded.one_foot_comment,
                 munger_quality = excluded.munger_quality,
                 munger_comment = excluded.munger_comment,
                 big_wave = excluded.big_wave,
                 big_wave_comment = excluded.big_wave_comment,
                 degenerate_accepted = excluded.degenerate_accepted,
                 matched_by = excluded.matched_by,
                 schema_mode = excluded.schema_mode,
                 prompt = excluded.prompt,
                 raw_response = excluded.raw_response,
                 scored_at = excluded.scored_at""",
            (ticker, quarter, model, rubric_version, json.dumps(dims), int(bool(not_rated)),
             inversion_scenario, inversion_severity, current_headwind,
             market_structure, market_structure_comment, one_foot_hurdle, one_foot_comment,
             most_recent_quarter, munger_quality, munger_comment, big_wave, big_wave_comment,
             int(bool(degenerate_accepted)), matched_by, schema_mode, prompt, raw_response,
             datetime.now(timezone.utc).isoformat()),
        )


def delete_score(ticker, quarter, model, rubric_version):
    """Deletes one ticker's stored score row for this exact (quarter,
    model, rubric_version) key - degenerate-response guard (3 Oct
    2026, owner-directed): used only by top100_engine.run_degenerate_
    sweep_once() to clear a row whose content was a sentinel-filled
    template, not a real judgement, so it's re-sent in the next batch
    rather than left on record as a genuine (if NOT RATED) score. A
    no-op (no error) if no such row exists."""
    with _conn() as conn:
        conn.execute(
            "DELETE FROM top100_scores WHERE ticker = ? AND quarter = ? "
            "AND model = ? AND rubric_version = ?",
            (ticker, quarter, model, rubric_version),
        )


def get_score(ticker, quarter, model, rubric_version):
    """{"ticker","quarter","model","rubric_version","dims","not_rated",
    "inversion_scenario","inversion_severity","current_headwind",
    "market_structure","market_structure_comment","one_foot_hurdle",
    "one_foot_comment","munger_quality","munger_comment","big_wave",
    "big_wave_comment","prompt","raw_response","scored_at"} for one
    ticker, or None if it hasn't been scored yet for this exact
    (quarter, model, rubric_version) - a row cached under a DIFFERENT
    rubric_version (e.g. a retired "v1") is never returned here, by
    design."""
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
    out["degenerate_accepted"] = bool(out.get("degenerate_accepted"))
    return out


def scores_for_quarter_model(quarter, model, rubric_version):
    """{ticker: score_dict, ...} for every ticker already scored this
    exact (quarter, model, rubric_version) - `quarter` here means an
    EXACT score-key string match, calendar-quarter-style ("2026Q3") or
    the newer results-driven "RP<date>"/"D<date>" keys alike. Kept for
    callers/fixtures that key off one specific quarter string (e.g. the
    RUBRIC_VERSION v4 fallback fixtures seeding "2026Q3" rows); the
    scoring-decision path (top100_engine._unscored_tickers()) and the
    render path (top100_render._enriched_pool()/_enriched_asx_
    extension()) no longer call this - see latest_scores_for_model()
    below, added for the results-driven refresh (27 Sep 2026) precisely
    because neither of those two callers can assume "the current quarter"
    is a meaningful score key any more."""
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
        d["degenerate_accepted"] = bool(d.get("degenerate_accepted"))
        out[d["ticker"]] = d
    return out


def latest_scores_for_model(model, rubric_version):
    """Results-driven Top 100 refresh (27 Sep 2026, owner-directed):
    {ticker: score_dict, ...} - for each ticker, its single most recent
    row (by scored_at) under this exact model + rubric_version,
    REGARDLESS of the quarter/score-key string value. Replaces every
    "current calendar quarter" lookup in both the scoring-decision path
    (top100_engine._unscored_tickers()) and the render path (top100_
    render._enriched_pool()/_enriched_asx_extension()) - a ticker can
    now carry more than one row under the same rubric (a genuine
    re-score after new results, or a rule-(c) age-triggered re-score),
    and this always returns the newest one. scores_for_quarter_model()
    above is kept, unchanged, for callers that still key off one exact
    quarter string."""
    with _conn() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM top100_scores WHERE model = ? AND rubric_version = ? "
            "ORDER BY scored_at ASC",
            (model, rubric_version),
        ).fetchall()
    out = {}
    for r in rows:
        d = dict(r)
        d["dims"] = json.loads(d.pop("dims_json"))
        d["not_rated"] = bool(d["not_rated"])
        d["degenerate_accepted"] = bool(d.get("degenerate_accepted"))
        out[d["ticker"]] = d  # ascending scored_at + overwrite -> newest row wins per ticker
    return out


def latest_score_previous_rubric(ticker, model, current_rubric_version):
    """Previous-rubric fallback (26 Sep 2026, owner-reported gap,
    RUBRIC_VERSION v4): the most recent score this ticker has under
    ANY rubric_version OTHER than `current_rubric_version`, so
    top100_render._enriched_pool() can render a row normally from it
    while the ticker has no CURRENT-rubric score yet (every pooled
    company, right after a rubric bump, until the next nightly run
    re-scores it) - the exact gap that used to blank the page/AWAITING-
    shelve every company for up to a day.

    Results-driven Top 100 refresh (27 Sep 2026): this used to try a
    "same quarter/model" match first, preferring an exact re-run of
    "this calendar quarter" under a retired rubric. That branch is
    dropped - a calendar quarter is no longer a meaningful axis to match
    on (see this module's own docstring for the new score-key scheme) -
    so this is now simply "the single most recent row under any other
    rubric", unconditionally. Behaviour for every existing caller/
    fixture is unchanged: a ticker scored more than once under a single
    retired rubric still resolves to its newest row there, and a ticker
    never scored under ANY other rubric still returns None so the
    caller falls through to today's existing AWAITING behaviour.
    Read-only - never writes, never deletes, never overwrites a score;
    a real current-rubric score, whenever it lands, is read by latest_
    scores_for_model() instead and always wins (see that function's own
    call site)."""
    with _conn() as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM top100_scores WHERE ticker = ? AND model = ? "
            "AND rubric_version != ? ORDER BY scored_at DESC LIMIT 1",
            (ticker, model, current_rubric_version),
        ).fetchone()
    if not row:
        return None
    out = dict(row)
    out["dims"] = json.loads(out.pop("dims_json"))
    out["not_rated"] = bool(out["not_rated"])
    out["degenerate_accepted"] = bool(out.get("degenerate_accepted"))
    return out


def latest_score_for_ticker(ticker, model):
    """Stored score viewer (3 Oct 2026, owner-directed): the single
    most recent row for `ticker` under `model`, across ALL rubric
    versions - unlike latest_scores_for_model()/latest_score_previous_
    rubric(), which are each scoped to one rubric_version (current or
    "any other"), this is "whatever this ticker's newest row actually
    is", so the owner's admin panel shows the real latest record even
    if it happens to sit under a now-retired rubric. None if this
    ticker has never been scored under this model at all. Read-only."""
    with _conn() as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM top100_scores WHERE ticker = ? AND model = ? "
            "ORDER BY scored_at DESC LIMIT 1",
            (ticker, model),
        ).fetchone()
    if not row:
        return None
    out = dict(row)
    out["dims"] = json.loads(out.pop("dims_json"))
    out["not_rated"] = bool(out["not_rated"])
    out["degenerate_accepted"] = bool(out.get("degenerate_accepted"))
    return out


# -----------------------------------------------------------------
# Batch state (at most one in-flight Batch API submission).
# -----------------------------------------------------------------

def save_batch_state(batch_id, quarter, model, custom_id_map):
    """`quarter` (results-driven Top 100 refresh, 27 Sep 2026): no
    longer a scoring key - top100_engine.submit_nightly_batch() now
    computes each entrant's own score key and stores it PER-ENTRANT
    inside `custom_id_map` (see that function's own docstring), applied
    per-ticker by poll_and_ingest_batch() at save time. This column is
    now a batch-level LABEL ONLY (today's submission date, for anyone
    reading the raw row) - nothing reads it back as a lookup key. Column
    kept as-is (no schema change) purely to avoid a table rebuild for
    what's now just a display string."""
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


def save_submitting_marker(quarter, model, custom_id_map):
    """Written BEFORE every batches.create() attempt - see that table's
    own comment above for why (max_retries=0, never silently double-
    submit on an ambiguous network failure)."""
    with _conn() as conn:
        conn.execute(
            """INSERT INTO top100_submitting_marker (id, attempted_at, quarter, model, custom_id_map_json)
                 VALUES (1, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                 attempted_at = excluded.attempted_at,
                 quarter = excluded.quarter,
                 model = excluded.model,
                 custom_id_map_json = excluded.custom_id_map_json""",
            (datetime.now(timezone.utc).isoformat(), quarter, model, json.dumps(custom_id_map)),
        )


def get_submitting_marker():
    """{"attempted_at","quarter","model","custom_id_map"} for a create()
    attempt whose outcome is still unknown, or None if none is
    pending."""
    with _conn() as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM top100_submitting_marker WHERE id = 1"
        ).fetchone()
    if not row or not row["attempted_at"]:
        return None
    out = dict(row)
    out["custom_id_map"] = json.loads(out.pop("custom_id_map_json") or "{}")
    return out


def clear_submitting_marker():
    with _conn() as conn:
        conn.execute("DELETE FROM top100_submitting_marker WHERE id = 1")


# -----------------------------------------------------------------
# Score failures (audit fixes, Commit 1, 30 Sep 2026) - per-ticker
# retry/strike memory so a batch result that failed isn't silently
# re-selected and resubmitted forever. See top100_engine.py's own
# TOP100_FAILURE_* constants docstring for the full root cause.
# -----------------------------------------------------------------

def record_score_failure(ticker, model, rubric_version, reason, most_recent_quarter=None,
                          schema_mode=None):
    """Upserts one failure for (ticker, model, rubric_version) -
    increments `attempts` (starts at 1 on the first failure), refreshes
    `failed_at` to now, and overwrites `reason` with the latest one
    (only the most recent failure reason is kept; the point of this
    table is retry gating, not a full failure history).

    `most_recent_quarter` (COMMIT 2 of instruction_top200_unrated_and_
    blank_replies_combined.md, 5 Oct 2026) - the pooled row's own value
    AT THE TIME of this failure, stored so top100_engine._failure_
    reentry_reason() can apply the same new_results trigger a score row
    gets. A None here NEVER blanks out a previously recorded value
    (COALESCE against the existing row) - only a real value ever
    overwrites it, same back-compat stance as every other nullable
    column added to this table's sibling top100_scores.

    `schema_mode` (COMMIT 4, same instruction) - the mode top100_
    engine.top200_schema_mode() read at submission time for the request
    this failure came from. Also COALESCE-preserved rather than blanked
    by an omitted later call, same reasoning as most_recent_quarter."""
    with _conn() as conn:
        conn.execute(
            """INSERT INTO top100_score_failures
                 (ticker, model, rubric_version, failed_at, reason, attempts,
                  most_recent_quarter, schema_mode)
                 VALUES (?, ?, ?, ?, ?, 1, ?, ?)
               ON CONFLICT(ticker, model, rubric_version) DO UPDATE SET
                 failed_at = excluded.failed_at,
                 reason = excluded.reason,
                 attempts = top100_score_failures.attempts + 1,
                 most_recent_quarter = COALESCE(excluded.most_recent_quarter, top100_score_failures.most_recent_quarter),
                 schema_mode = COALESCE(excluded.schema_mode, top100_score_failures.schema_mode)""",
            (ticker, model, rubric_version, datetime.now(timezone.utc).isoformat(), reason,
             most_recent_quarter, schema_mode),
        )


def clear_score_failure(ticker, model, rubric_version):
    """Deletes any failure row for (ticker, model, rubric_version) - a
    successful score clears this ticker's strike count entirely, so a
    ticker that fails once and later succeeds isn't left shadowed by a
    stale attempts tally. A no-op (no error) if no row exists."""
    with _conn() as conn:
        conn.execute(
            "DELETE FROM top100_score_failures WHERE ticker = ? AND model = ? AND rubric_version = ?",
            (ticker, model, rubric_version),
        )


def score_failures_for_model(model, rubric_version):
    """{ticker: {"failed_at", "reason", "attempts", "most_recent_quarter",
    "schema_mode"}, ...} for every ticker with a recorded failure under
    this exact (model, rubric_version) - one bulk read for top100_
    engine._unscored_tickers() to filter against, rather than a query
    per pooled ticker. most_recent_quarter (COMMIT 2) and schema_mode
    (COMMIT 4) are each None for any row saved before that column
    existed or by a caller that didn't pass it."""
    with _conn() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM top100_score_failures WHERE model = ? AND rubric_version = ?",
            (model, rubric_version),
        ).fetchall()
    return {r["ticker"]: {"failed_at": r["failed_at"], "reason": r["reason"], "attempts": r["attempts"],
                           "most_recent_quarter": r["most_recent_quarter"],
                           "schema_mode": r["schema_mode"]}
            for r in rows}


def convert_score_failure_reason(model, rubric_version, from_reason, to_reason):
    """Plain UPDATE (not record_score_failure()'s upsert-and-increment)
    that rewrites `reason` for every row currently stored under
    `from_reason`, leaving `attempts` and `failed_at` untouched - a
    one-off reclassification migration, not a new failure event.
    Returns the list of tickers converted."""
    with _conn() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT ticker FROM top100_score_failures WHERE model = ? AND rubric_version = ? AND reason = ?",
            (model, rubric_version, from_reason),
        ).fetchall()
        tickers = [r["ticker"] for r in rows]
        if tickers:
            conn.execute(
                "UPDATE top100_score_failures SET reason = ? WHERE model = ? AND rubric_version = ? AND reason = ?",
                (to_reason, model, rubric_version, from_reason),
            )
    return tickers


# -----------------------------------------------------------------
# Daily submission cap (audit fixes, Commit 1, 30 Sep 2026) - hard
# spend ceiling, keyed by UTC date. See top100_engine.submit_nightly_
# batch()'s own docstring for how the two counters are enforced.
# -----------------------------------------------------------------

def record_daily_submission(utc_date, entrant_count):
    """Increments today's batch count by 1 and entrant count by
    `entrant_count` - called once per successfully-submitted batch,
    never on a refused/failed submission attempt."""
    with _conn() as conn:
        conn.execute(
            """INSERT INTO top100_daily_submissions (utc_date, batches, entrants)
                 VALUES (?, 1, ?)
               ON CONFLICT(utc_date) DO UPDATE SET
                 batches = top100_daily_submissions.batches + 1,
                 entrants = top100_daily_submissions.entrants + excluded.entrants""",
            (utc_date, entrant_count),
        )


def get_daily_submission_state(utc_date):
    """{"batches", "entrants"} submitted so far for this UTC date -
    {"batches": 0, "entrants": 0} if nothing has been submitted yet
    today (no row written), never None - callers compare directly
    against the TOP100_MAX_* caps with no extra None-check needed."""
    with _conn() as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM top100_daily_submissions WHERE utc_date = ?", (utc_date,)
        ).fetchone()
    if not row:
        return {"batches": 0, "entrants": 0}
    return {"batches": row["batches"], "entrants": row["entrants"]}


# -----------------------------------------------------------------
# Ingest cost log (honest cost accounting, 1 Oct 2026, owner-directed) -
# one row per ingested batch, written by top100_engine.poll_and_
# ingest_batch() right after it logs the same figures, so the Admin
# Dashboard's own cost panel has a persisted number to sum instead of
# re-parsing log lines.
# -----------------------------------------------------------------

def record_ingest_cost(batch_id, scored, failed, input_tokens, cache_creation_tokens,
                        cache_read_tokens, output_tokens, cost_usd, custom_id_map=None):
    """Upserts one batch's ingest telemetry - re-ingesting the same
    batch_id (shouldn't happen in practice; top100_batch_state is a
    singleton and is cleared before this is called) overwrites rather
    than double-counts.

    Batch inspector (3 Oct 2026, owner-directed): `custom_id_map` is the
    SAME {custom_id: {"entrants": {ticker: {...}}}} dict top100_batch_
    state held for this batch before poll_and_ingest_batch() cleared it
    - passed through here (still in scope at the one call site, right
    before that clear) purely so the Admin Dashboard's Batch inspector
    panel can still answer "which tickers was this request EXPECTED to
    carry" for a batch that has already been ingested. None (the
    default) for any caller that doesn't have it on hand - stored as
    NULL, and the inspector reports "not available" for those rows
    rather than guessing."""
    with _conn() as conn:
        conn.execute(
            """INSERT INTO top100_ingest_log
                 (batch_id, ingested_at, scored, failed, input_tokens,
                  cache_creation_tokens, cache_read_tokens, output_tokens, cost_usd,
                  custom_id_map_json)
                 VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(batch_id) DO UPDATE SET
                 ingested_at = excluded.ingested_at,
                 scored = excluded.scored,
                 failed = excluded.failed,
                 input_tokens = excluded.input_tokens,
                 cache_creation_tokens = excluded.cache_creation_tokens,
                 cache_read_tokens = excluded.cache_read_tokens,
                 output_tokens = excluded.output_tokens,
                 cost_usd = excluded.cost_usd,
                 custom_id_map_json = excluded.custom_id_map_json""",
            (batch_id, datetime.now(timezone.utc).isoformat(), scored, failed, input_tokens,
             cache_creation_tokens, cache_read_tokens, output_tokens, cost_usd,
             json.dumps(custom_id_map) if custom_id_map is not None else None),
        )


def most_recent_ingested_batch_id():
    """The batch_id of the most recently ingested Top 100 batch (by
    ingested_at), or None if nothing has ever ingested - the Admin
    Dashboard's Batch inspector panel uses this as its default input so
    the owner doesn't have to copy a batch id out of the Railway log by
    hand for the common case (inspecting last night's own run)."""
    with _conn() as conn:
        row = conn.execute(
            "SELECT batch_id FROM top100_ingest_log ORDER BY ingested_at DESC LIMIT 1"
        ).fetchone()
    return row[0] if row else None


def expected_tickers_for_batch(batch_id):
    """{custom_id: [ticker, ...], ...} for the given batch_id, from the
    custom_id_map this batch was ingested with (see record_ingest_
    cost()'s own docstring) - or None if that batch's row has no
    custom_id_map_json (ingested before this column existed, or
    batch_id not found at all). The Batch inspector panel uses this to
    tell a partially-matched request from one whose expected count is
    simply unknown."""
    with _conn() as conn:
        row = conn.execute(
            "SELECT custom_id_map_json FROM top100_ingest_log WHERE batch_id = ?",
            (batch_id,),
        ).fetchone()
    if not row or not row[0]:
        return None
    try:
        raw = json.loads(row[0])
    except (TypeError, ValueError):
        return None
    out = {}
    for custom_id, entry in raw.items():
        entrants = (entry or {}).get("entrants") if isinstance(entry, dict) else None
        out[custom_id] = sorted(entrants.keys()) if entrants else []
    return out


def schema_mode_for_batch(batch_id):
    """{custom_id: schema_mode, ...} for the given batch_id, from the
    same custom_id_map_json that expected_tickers_for_batch() reads -
    or None if that batch's row has no custom_id_map_json (ingested
    before this column existed, or batch_id not found at all).

    A custom_id entry written before COMMIT 4 of instruction_top200_
    unrated_and_blank_replies_combined.md has no "schema_mode" key at
    all (it was submitted under legacy-only code) - that entry reads
    as "legacy" here, same as top100_engine.top200_schema_mode()'s own
    default, so the Batch inspector's schema_mode/packed_or_solo
    columns are correct for historical batches without a special
    case."""
    with _conn() as conn:
        row = conn.execute(
            "SELECT custom_id_map_json FROM top100_ingest_log WHERE batch_id = ?",
            (batch_id,),
        ).fetchone()
    if not row or not row[0]:
        return None
    try:
        raw = json.loads(row[0])
    except (TypeError, ValueError):
        return None
    out = {}
    for custom_id, entry in raw.items():
        out[custom_id] = (entry or {}).get("schema_mode", "legacy") if isinstance(entry, dict) else "legacy"
    return out


def ingest_cost_last_n_days(days=7):
    """{"batches", "scored", "failed", "cost_usd", "cache_creation_
    tokens", "cache_read_tokens"} summed over every ingest-log row
    whose ingested_at falls within the last `days` days (UTC, now
    inclusive) - {"batches": 0, "scored": 0, "failed": 0, "cost_usd":
    0.0, "cache_creation_tokens": 0, "cache_read_tokens": 0} if nothing
    has ingested yet in that window, never None, so the Admin
    Dashboard panel needs no extra None-check. Plain string comparison
    against ISO-format ingested_at (same convention every other date-
    keyed table/query in this module already relies on)."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with _conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(scored), 0), COALESCE(SUM(failed), 0), "
            "COALESCE(SUM(cost_usd), 0), COALESCE(SUM(cache_creation_tokens), 0), "
            "COALESCE(SUM(cache_read_tokens), 0) "
            "FROM top100_ingest_log WHERE ingested_at >= ?",
            (cutoff,),
        ).fetchone()
    return {
        "batches": row[0], "scored": row[1], "failed": row[2], "cost_usd": row[3],
        "cache_creation_tokens": row[4], "cache_read_tokens": row[5],
    }


# -----------------------------------------------------------------
# Newcomer persistence filter (1 Oct 2026, owner decision, v6 cost
# task) - see pool_presence's own CREATE TABLE comment in _conn()
# above for what this tracks and why.
# -----------------------------------------------------------------

def update_pool_presence(tickers, utc_date):
    """Called once per nightly pool selection (top100_engine.select_
    top100_pool(), right after save_pool()) with every ticker in THAT
    selection - `tickers`: the full pool+extension ticker list,
    `utc_date`: that selection's own as_of date ("YYYY-MM-DD"). For
    each ticker:
      - no existing row (never seen before) -> starts at consecutive_
        nights=1.
      - last_seen_utc_date was exactly the calendar day before
        `utc_date` -> increments the streak by 1.
      - last_seen_utc_date IS `utc_date` already (this ticker's
        presence was already updated today - shouldn't happen in
        practice, selection runs once a night) -> left unchanged.
      - any other gap (the ticker dropped out of the pool for one or
        more nights and has now reappeared, or its stored date is
        unparseable) -> RESETS the streak to 1, per the task's own
        explicit "reset to 1 when a ticker drops out and reappears"
        rule. first_seen_utc_date is never touched once set - it
        records when this ticker was first ever seen, not when its
        current streak started.
    Never touches a ticker that ISN'T in `tickers` - a ticker currently
    out of the pool simply keeps its last recorded row untouched until
    (if ever) it reappears in a future selection."""
    if not tickers:
        return
    try:
        target_date = datetime.strptime(utc_date, "%Y-%m-%d").date()
    except ValueError:
        return
    with _conn() as conn:
        conn.row_factory = sqlite3.Row
        placeholders = ",".join("?" * len(tickers))
        existing = {
            r["ticker"]: r for r in conn.execute(
                f"SELECT * FROM pool_presence WHERE ticker IN ({placeholders})", tickers,
            ).fetchall()
        }
        rows_to_write = []
        for ticker in tickers:
            row = existing.get(ticker)
            if row is None:
                rows_to_write.append((ticker, utc_date, 1, utc_date))
                continue
            try:
                last_dt = datetime.strptime(row["last_seen_utc_date"], "%Y-%m-%d").date()
                gap_days = (target_date - last_dt).days
            except (ValueError, TypeError):
                gap_days = None
            if gap_days == 1:
                consecutive = row["consecutive_nights"] + 1
            elif gap_days == 0:
                consecutive = row["consecutive_nights"]
            else:
                consecutive = 1
            rows_to_write.append((ticker, row["first_seen_utc_date"], consecutive, utc_date))
        conn.executemany(
            """INSERT INTO pool_presence (ticker, first_seen_utc_date, consecutive_nights, last_seen_utc_date)
                 VALUES (?, ?, ?, ?)
               ON CONFLICT(ticker) DO UPDATE SET
                 consecutive_nights = excluded.consecutive_nights,
                 last_seen_utc_date = excluded.last_seen_utc_date""",
            rows_to_write,
        )


def pool_presence_map(tickers):
    """{ticker: consecutive_nights} for every ticker in `tickers` that
    has a pool_presence row - a ticker not present in the returned dict
    has never been seen by update_pool_presence()/seed_pool_presence()
    at all, which every caller (top100_engine._true_newcomer_gated())
    treats as 0 consecutive nights - fully gated, same as a ticker on
    its own first night."""
    if not tickers:
        return {}
    with _conn() as conn:
        placeholders = ",".join("?" * len(tickers))
        rows = conn.execute(
            f"SELECT ticker, consecutive_nights FROM pool_presence WHERE ticker IN ({placeholders})",
            tickers,
        ).fetchall()
    return {r[0]: r[1] for r in rows}


def seed_pool_presence(ticker_to_nights, utc_date):
    """One-off v6 seeding helper (top100_engine.seed_pool_presence_
    for_v6_once()'s own writer, called exactly once - see that
    function's own docstring for why) - `ticker_to_nights`: {ticker:
    consecutive_nights (1 or 3)}. INSERT OR IGNORE so a ticker that
    somehow already has a pool_presence row (update_pool_presence()
    having already run for it before this seeding got a chance to)
    is never clobbered by this one-time baseline."""
    if not ticker_to_nights:
        return
    with _conn() as conn:
        conn.executemany(
            """INSERT OR IGNORE INTO pool_presence
                 (ticker, first_seen_utc_date, consecutive_nights, last_seen_utc_date)
                 VALUES (?, ?, ?, ?)""",
            [(ticker, utc_date, nights, utc_date) for ticker, nights in ticker_to_nights.items()],
        )
