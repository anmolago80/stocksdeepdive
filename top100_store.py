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
                  value_score_source_row, pool_selection_rule)
                 VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                 pool_selection_rule = excluded.pool_selection_rule""",
            [
                (as_of, r["ticker"], r.get("company_name"), r.get("universe"),
                 r.get("value_score"), r.get("mos_pct"), r.get("price"),
                 r.get("intrinsic_value"), r.get("currency"), r.get("psychology"),
                 r.get("sector"), r.get("dividend_yield_pct"), r.get("most_recent_quarter"),
                 r.get("also_trades_as"), int(bool(r.get("asx_extension"))),
                 r.get("generated_at"), r.get("source_universe_generated_at"),
                 int(bool(r.get("stale_valuation"))), r.get("value_score_source_row"),
                 r.get("pool_selection_rule"))
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


# -----------------------------------------------------------------
# Scores (AI qualitative scoring cache).
# -----------------------------------------------------------------

def save_score(ticker, quarter, model, rubric_version, dims, not_rated,
                inversion_scenario, inversion_severity, prompt, raw_response,
                current_headwind=None, market_structure=None, market_structure_comment=None,
                one_foot_hurdle=None, one_foot_comment=None, most_recent_quarter=None,
                munger_quality=None, munger_comment=None, big_wave=None, big_wave_comment=None):
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
    reproducibility (the task's own instruction) - never truncated."""
    with _conn() as conn:
        conn.execute(
            """INSERT INTO top100_scores
                 (ticker, quarter, model, rubric_version, dims_json, not_rated,
                  inversion_scenario, inversion_severity, current_headwind,
                  market_structure, market_structure_comment,
                  one_foot_hurdle, one_foot_comment, most_recent_quarter,
                  munger_quality, munger_comment, big_wave, big_wave_comment,
                  prompt, raw_response, scored_at)
                 VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                 prompt = excluded.prompt,
                 raw_response = excluded.raw_response,
                 scored_at = excluded.scored_at""",
            (ticker, quarter, model, rubric_version, json.dumps(dims), int(bool(not_rated)),
             inversion_scenario, inversion_severity, current_headwind,
             market_structure, market_structure_comment, one_foot_hurdle, one_foot_comment,
             most_recent_quarter, munger_quality, munger_comment, big_wave, big_wave_comment,
             prompt, raw_response, datetime.now(timezone.utc).isoformat()),
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


# -----------------------------------------------------------------
# Score failures (audit fixes, Commit 1, 30 Sep 2026) - per-ticker
# retry/strike memory so a batch result that failed isn't silently
# re-selected and resubmitted forever. See top100_engine.py's own
# TOP100_FAILURE_* constants docstring for the full root cause.
# -----------------------------------------------------------------

def record_score_failure(ticker, model, rubric_version, reason):
    """Upserts one failure for (ticker, model, rubric_version) -
    increments `attempts` (starts at 1 on the first failure), refreshes
    `failed_at` to now, and overwrites `reason` with the latest one
    (only the most recent failure reason is kept; the point of this
    table is retry gating, not a full failure history)."""
    with _conn() as conn:
        conn.execute(
            """INSERT INTO top100_score_failures (ticker, model, rubric_version, failed_at, reason, attempts)
                 VALUES (?, ?, ?, ?, ?, 1)
               ON CONFLICT(ticker, model, rubric_version) DO UPDATE SET
                 failed_at = excluded.failed_at,
                 reason = excluded.reason,
                 attempts = top100_score_failures.attempts + 1""",
            (ticker, model, rubric_version, datetime.now(timezone.utc).isoformat(), reason),
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
    """{ticker: {"failed_at", "reason", "attempts"}, ...} for every
    ticker with a recorded failure under this exact (model, rubric_
    version) - one bulk read for top100_engine._unscored_tickers() to
    filter against, rather than a query per pooled ticker."""
    with _conn() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM top100_score_failures WHERE model = ? AND rubric_version = ?",
            (model, rubric_version),
        ).fetchall()
    return {r["ticker"]: {"failed_at": r["failed_at"], "reason": r["reason"], "attempts": r["attempts"]}
            for r in rows}


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
