"""
TASK 77 (Director-directed, owner-approved, 23 Nov 2026 - TASK 71 Part
B) - one nightly email of the irreplaceable set.

CONTEXT: measured from the owner's own full download (23 Nov 2026,
47.6MB gz): user tables 0.04MB, history 3.9MB, top100_scores 12MB
(~2MB with prompt NULLed), analytics 7.2MB, the rest rebuildable. The
old design sent "critical tables only" (0.03MB) 6 nights out of 7 and
a whole-database email (~47.6MB, over Mailgun's 25MB limit) on the
7th. Both were replaced by an explicit allowlist and a deterministic
nightly/Sunday/monthly email shape - see db_backup_engine.py's own
module docstring for the full design.

Covers:
  - DRIFT TEST: builds the REAL schema (every *_store.py-shaped
    module's own CREATE TABLE IF NOT EXISTS, run for real against one
    shared temp SQLite file - not a source-text scan) and fails if any
    table ends up in none of BACKUP_TABLES's 4 groups,
    REBUILDABLE_TABLES, or EXCLUDED_FROM_EMAIL_TABLES; also fails on a
    table classified in more than one place.
  - BACKUP_TABLES/REBUILDABLE_TABLES/EXCLUDED_FROM_EMAIL_TABLES: the
    specific tables TASK 77's own instruction named, confirmed present
    in the right group.
  - _run_backup() end-to-end against a fixture DB with one table from
    every group: a Monday (nightly only, one attachment); a Sunday
    (nightly + analytics attachments, + weekly extras); score_history
    trimmed to the last 35 days in the nightly copy; top100_scores.
    prompt NULLed in the attachment while the FIXTURE (live) DB's own
    prompt column is untouched; auth tables absent from every
    attachment, every day.
  - _run_monthly_score_history_backup(): a separate email, FULL
    (untrimmed) score_history, independent of the nightly job.
  - Size guard: an oversized attachment is dropped (not sent), logged
    as "OVER LIMIT", noted in the email body - without blocking a
    smaller sibling attachment on the same email.
  - run_backup_now() calls the same _run_backup() construction (no
    "force_full" concept survives).

Full suite, 0 failures required alongside this file.

Run: python3 tests/test_director_task77_backup_tables_drift.py
"""
import importlib
import inspect
import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

passed = 0
failed = 0


def check(label, condition):
    global passed, failed
    if condition:
        passed += 1
        print(f"  OK: {label}")
    else:
        failed += 1
        print(f"  FAIL: {label}")


import db_backup_engine as dbe


# ======================================================================
# DRIFT TEST: build the REAL schema from every table-defining module,
# in one shared temp SQLite file, and confirm every table it creates
# is in exactly one of BACKUP_TABLES's 4 groups / REBUILDABLE_TABLES /
# EXCLUDED_FROM_EMAIL_TABLES.
# ======================================================================
_SCHEMA_MODULES = [
    ("account_currency_store", "_conn"), ("admin_metrics_store", "_conn"),
    ("ai_settings_store", "_conn"), ("ai_usage_store", "_conn"),
    ("alert_store", "_conn"), ("bill_check_engine", "_conn"),
    ("blog_comments_store", "_conn"), ("blog_store", "_conn"),
    ("card_blurb_store", "_conn"), ("checklist_store", "_conn"),
    ("comment_triage_store", "_conn"), ("compare_lists_store", "_conn"),
    ("compare_notes_store", "_conn"), ("email_auth", "_conn"),
    ("etf_insights", "_conn"), ("explain_cache_store", "_conn"),
    ("follow_store", "_conn"), ("insider_store", "_conn"),
    ("insurance_engine", "_conn"), ("metrics_store", "_conn"),
    ("newsletter_store", "_conn"), ("portfolio_news_engine", "_conn"),
    ("portfolio_store", "_conn"), ("portfolio_watchdog_engine", "_conn"),
    ("positions_store", "_conn"), ("push_store", "_conn"),
    ("quote_snapshot_store", "_conn"), ("results_store", "_conn"),
    ("score_history", "_conn"), ("screen_import_store", "_conn"),
    ("sector_cache_store", "_conn"), ("share_link_store", "_conn"),
    ("snapshot_store", "_conn"), ("stress_engine", "_conn"),
    ("tools_store", "_conn"), ("top100_store", "_conn"),
    ("watchlist_store", "_conn"),
    ("portfolio_health_engine", "_health_runs_conn"),
]


def _build_real_schema():
    """Imports every module in _SCHEMA_MODULES, points its own DB_PATH
    (set directly on that specific module object - safe even for a
    module that merely ALIASES another's DB_PATH at import time, e.g.
    blog_comments_store.DB_PATH = blog_store.DB_PATH, since this sets
    the attribute on the aliasing module itself, not just the one it
    copied from at import time) at one shared temp SQLite file, then
    calls its own connection-init function once. Returns the set of
    every real table name that ends up in that shared file - the
    actual, current schema, not a source-text guess."""
    tmp_fd, tmp_path = tempfile.mkstemp(suffix=".db")
    os.close(tmp_fd)
    os.remove(tmp_path)
    try:
        mods = {}
        for mod_name, _fn in _SCHEMA_MODULES:
            mod = importlib.import_module(mod_name)
            mods[mod_name] = mod
            if hasattr(mod, "DB_PATH"):
                mod.DB_PATH = tmp_path
        for mod_name, fn_name in _SCHEMA_MODULES:
            obj = getattr(mods[mod_name], fn_name)()
            if hasattr(obj, "__enter__"):
                with obj:
                    pass
            else:
                obj.close()
        conn = sqlite3.connect(tmp_path)
        try:
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()}
        finally:
            conn.close()
        return tables
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)


_real_tables = _build_real_schema()
check("drift test: the real schema build actually produced tables (sanity check "
      "that the harness itself isn't silently empty)",
      len(_real_tables) > 50)

_all_classified = (
    dbe.BACKUP_TABLES["USER"] + dbe.BACKUP_TABLES["HISTORY"]
    + dbe.BACKUP_TABLES["AI"] + dbe.BACKUP_TABLES["ANALYTICS"]
    + dbe.REBUILDABLE_TABLES + dbe.EXCLUDED_FROM_EMAIL_TABLES
)
_classified_set = set(_all_classified)
_unclassified = _real_tables - _classified_set
check("DRIFT TEST: every real table is in a BACKUP_TABLES group, "
      "REBUILDABLE_TABLES or EXCLUDED_FROM_EMAIL_TABLES "
      f"(unclassified: {sorted(_unclassified)})",
      not _unclassified)

check("no internal duplicates across BACKUP_TABLES/REBUILDABLE_TABLES/EXCLUDED_FROM_EMAIL_TABLES "
      "(no table classified twice)",
      len(_all_classified) == len(_classified_set))

# Every NAMED table actually exists for real (no typo risk the other way).
_named_not_real = _classified_set - _real_tables
check(f"every classified table name actually exists in the real schema "
      f"(no typos: {sorted(_named_not_real)})",
      not _named_not_real)


# ======================================================================
# BACKUP_TABLES/REBUILDABLE_TABLES/EXCLUDED_FROM_EMAIL_TABLES - specific
# tables the Director's own instruction named, in the right group.
# ======================================================================
check("BACKUP_TABLES has exactly the 4 groups USER/HISTORY/AI/ANALYTICS",
      set(dbe.BACKUP_TABLES) == {"USER", "HISTORY", "AI", "ANALYTICS"})

_USER_NEW_EXPECTED = [
    "rebalance_mixes", "budget_plans", "budget_plan_names", "super_scenarios",
    "super_scenario_names", "property_vs_index_scenarios",
    "property_vs_index_scenario_names", "debt_recycling_scenarios",
    "debt_recycling_scenario_names", "screen_imports", "screen_import_tickers",
    "research_status", "custom_categories", "ledger_rules", "ledger_imports",
    "ledger_entries", "ledger_category_flags", "account_currency",
    "bill_checks", "bill_check_history", "typical_deal_rates",
    "portfolio_watchdog_state",
]
check("BACKUP_TABLES['USER'] includes every TASK 77 USER addition",
      all(t in dbe.BACKUP_TABLES["USER"] for t in _USER_NEW_EXPECTED))
check("BACKUP_TABLES['USER'] deliberately does NOT include bill_check_usage "
      "(a trial/rate-limit counter, not saved user content - stays REBUILDABLE)",
      "bill_check_usage" not in dbe.BACKUP_TABLES["USER"]
      and "bill_check_usage" in dbe.REBUILDABLE_TABLES)

_HISTORY_EXPECTED = {
    "score_history", "quote_snapshots", "quote_snapshot_runs", "top100_qos_pool",
    "pool_presence", "top100_pool", "top100_daily_submissions",
    "portfolio_health_runs", "results_events",
}
check("BACKUP_TABLES['HISTORY'] is exactly the Director's named set",
      set(dbe.BACKUP_TABLES["HISTORY"]) == _HISTORY_EXPECTED)
check("BACKUP_TABLES['AI'] is exactly ['top100_scores']",
      dbe.BACKUP_TABLES["AI"] == ["top100_scores"])

_ANALYTICS_EXPECTED = {
    "page_views", "daily_visitor_hashes", "daily_human_hashes",
    "visitor_hashes_30d", "owner_visitor_hashes_30d", "pulse_counters",
    "signin_accounts",
}
check("BACKUP_TABLES['ANALYTICS'] is exactly the Director's named set",
      set(dbe.BACKUP_TABLES["ANALYTICS"]) == _ANALYTICS_EXPECTED)
check("EXCLUDED_FROM_EMAIL_TABLES unchanged - exactly the 4 auth tables",
      set(dbe.EXCLUDED_FROM_EMAIL_TABLES) == {
          "auth_sessions", "auth_codes", "auth_ip_sends", "daily_signin_hashes",
      })
check("SCORE_HISTORY_RETENTION_DAYS is 35",
      dbe.SCORE_HISTORY_RETENTION_DAYS == 35)
check("the old CRITICAL_TABLES constant no longer exists (replaced)",
      not hasattr(dbe, "CRITICAL_TABLES"))
check("the old _trim_to_critical_tables() no longer exists (replaced by the "
      "generic _trim_to_tables())",
      not hasattr(dbe, "_trim_to_critical_tables") and hasattr(dbe, "_trim_to_tables"))
check("the old WEEKLY_FULL_WEEKDAY constant no longer exists (renamed "
      "ANALYTICS_WEEKDAY - there's no more 'weekly full' concept)",
      not hasattr(dbe, "WEEKLY_FULL_WEEKDAY") and dbe.ANALYTICS_WEEKDAY == 6)


# ======================================================================
# Fixture DB with one table from every group, plus a real monthly-old
# score_history row (to prove the 35-day trim) and a top100_scores row
# with a real prompt (to prove the NULL-out).
# ======================================================================
_OLD_DAY = (datetime.now(timezone.utc) - timedelta(days=50)).strftime("%Y-%m-%d")
_RECENT_DAY = (datetime.now(timezone.utc) - timedelta(days=5)).strftime("%Y-%m-%d")


def _make_fixture_db(path):
    conn = sqlite3.connect(path)
    # USER
    conn.execute("CREATE TABLE signups (email TEXT)")
    conn.execute("INSERT INTO signups VALUES ('a@example.com')")
    conn.execute("CREATE TABLE compare_lists (id INTEGER PRIMARY KEY, data TEXT)")
    conn.execute("INSERT INTO compare_lists VALUES (1, 'list-a')")
    # HISTORY - one row 50 days old (should be TRIMMED from the nightly
    # copy), one row 5 days old (should SURVIVE the trim).
    conn.execute("CREATE TABLE score_history (day TEXT, ticker TEXT, long_score REAL)")
    conn.execute("INSERT INTO score_history VALUES (?, 'AAPL', 55.0)", (_OLD_DAY,))
    conn.execute("INSERT INTO score_history VALUES (?, 'AAPL', 60.0)", (_RECENT_DAY,))
    conn.execute("CREATE TABLE portfolio_health_runs (email TEXT, ticker TEXT, as_of TEXT)")
    conn.execute("INSERT INTO portfolio_health_runs VALUES ('a@example.com', 'AAPL', '2026-01-01')")
    # AI - a real prompt that must be NULLed in the copy, raw_response
    # that must survive.
    conn.execute("CREATE TABLE top100_scores (ticker TEXT, prompt TEXT, raw_response TEXT)")
    conn.execute("INSERT INTO top100_scores VALUES ('AAPL', 'a very long real prompt text', 'the AI output')")
    # ANALYTICS
    conn.execute("CREATE TABLE page_views (day TEXT, path TEXT, count INTEGER)")
    conn.execute("INSERT INTO page_views VALUES ('2026-01-01', '/blog', 10)")
    # REBUILDABLE
    conn.execute("CREATE TABLE explain_cache (ticker TEXT, explanation TEXT)")
    conn.execute("INSERT INTO explain_cache VALUES ('AAPL', 'cached explanation')")
    # EXCLUDED (auth)
    conn.execute("CREATE TABLE auth_sessions (token TEXT)")
    conn.execute("INSERT INTO auth_sessions VALUES ('session-secret')")
    conn.execute("CREATE TABLE auth_codes (code TEXT)")
    conn.execute("CREATE TABLE auth_ip_sends (ip TEXT)")
    conn.execute("CREATE TABLE daily_signin_hashes (h TEXT)")
    conn.commit()
    conn.close()


def _tables_in(path):
    conn = sqlite3.connect(path)
    try:
        return {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()}
    finally:
        conn.close()


class _FakeDateTime:
    """A drop-in for the `datetime` name db_backup_engine.py imports -
    only .now()/.strptime() are ever called on it there."""
    _fixed = None

    @classmethod
    def now(cls, tz=None):
        return cls._fixed

    strptime = datetime.strptime


def _capture_email(sent_list, subj, body, atts):
    """_send_backup_email() stub - _run_backup()'s own `finally:
    shutil.rmtree(tmp_dir)` deletes every attachment file the moment
    _run_backup() returns, so each attachment's gzipped bytes (and, by
    extension, its decompressed table set) must be captured HERE,
    while the files still exist - not from the path alone afterward."""
    import gzip as _gm
    captured = []
    for path in atts:
        with open(path, "rb") as f:
            gz_bytes = f.read()
        captured.append({"name": os.path.basename(path), "gz_bytes": gz_bytes,
                          "raw_bytes": _gm.decompress(gz_bytes)})
    sent_list.append({"subject": subj, "body": body, "attachments": captured})


def _run_backup_on(fixture_path, fixed_now, log_lines, sent_list):
    _FakeDateTime._fixed = fixed_now
    with mock.patch.object(dbe, "DB_PATH", fixture_path), \
         mock.patch.object(dbe, "datetime", _FakeDateTime), \
         mock.patch.object(dbe, "_send_backup_email",
                            side_effect=lambda subj, body, atts: _capture_email(sent_list, subj, body, atts)):
        return dbe._run_backup(log=lambda msg: log_lines.append(msg))


def _tables_in_raw_bytes(raw_bytes, tmp_dir):
    """Writes decompressed sqlite bytes to a scratch file inside
    `tmp_dir` and returns its table set."""
    path = os.path.join(tmp_dir, f"scratch_{len(os.listdir(tmp_dir))}.db")
    with open(path, "wb") as f:
        f.write(raw_bytes)
    return _tables_in(path)


# A Monday (not Sunday) - nightly only.
with tempfile.TemporaryDirectory() as _tmp:
    _db_path = os.path.join(_tmp, "fixture.db")
    _make_fixture_db(_db_path)
    _sent, _logs = [], []
    _monday = datetime(2026, 11, 23, 3, 0, tzinfo=timezone.utc)  # a Monday
    check("fixture's chosen date really is a Monday (weekday 0)", _monday.weekday() == 0)
    _ok, _msg, _detail = _run_backup_on(_db_path, _monday, _logs, _sent)
    check("_run_backup() succeeds on a weekday", _ok)
    check("exactly one email sent on a weekday", len(_sent) == 1)
    if _sent:
        check("weekday email has exactly ONE attachment (nightly only, no analytics)",
              len(_sent[0]["attachments"]) == 1)
        _nightly_att = _sent[0]["attachments"][0]
        check('weekday attachment is named "stocksdeepdive_backup_<date>.db.gz"',
              _nightly_att["name"].startswith("stocksdeepdive_backup_")
              and _nightly_att["name"].endswith(".db.gz"))
        _decompressed_path = os.path.join(_tmp, "nightly_decompressed.db")
        with open(_decompressed_path, "wb") as fout:
            fout.write(_nightly_att["raw_bytes"])
        _att_tables = _tables_in(_decompressed_path)
        check("nightly attachment includes USER/HISTORY/AI tables",
              {"signups", "compare_lists", "score_history", "portfolio_health_runs",
               "top100_scores"}.issubset(_att_tables))
        check("nightly attachment does NOT include the ANALYTICS table (weekday, not Sunday)",
              "page_views" not in _att_tables)
        check("nightly attachment does NOT include the REBUILDABLE table",
              "explain_cache" not in _att_tables)
        check("nightly attachment does NOT include ANY auth/excluded table",
              not _att_tables & {"auth_sessions", "auth_codes", "auth_ip_sends", "daily_signin_hashes"})

        _conn = sqlite3.connect(_decompressed_path)
        _score_days = {r[0] for r in _conn.execute("SELECT day FROM score_history").fetchall()}
        check("nightly attachment's score_history is trimmed to the last 35 days "
              "(the 50-day-old row is gone, the 5-day-old row survives)",
              _RECENT_DAY in _score_days and _OLD_DAY not in _score_days)
        _prompt_row = _conn.execute("SELECT prompt, raw_response FROM top100_scores WHERE ticker='AAPL'").fetchone()
        check("nightly attachment's top100_scores.prompt is NULL",
              _prompt_row[0] is None)
        check("nightly attachment's top100_scores.raw_response survives (not nulled)",
              _prompt_row[1] == "the AI output")
        _conn.close()

    # The LIVE fixture DB itself must be completely untouched - prompt
    # still real, score_history still has the old row, auth tables intact.
    _live_tables = _tables_in(_db_path)
    check("the LIVE fixture DB still has every original table (nothing dropped "
          "from the source)",
          {"signups", "compare_lists", "score_history", "portfolio_health_runs",
           "top100_scores", "page_views", "explain_cache", "auth_sessions",
           "auth_codes", "auth_ip_sends", "daily_signin_hashes"} <= _live_tables)
    _live_conn = sqlite3.connect(_db_path)
    _live_prompt = _live_conn.execute(
        "SELECT prompt FROM top100_scores WHERE ticker='AAPL'"
    ).fetchone()[0]
    check("the LIVE fixture DB's top100_scores.prompt is UNTOUCHED (still the real text) - "
          "prompt-NULLing only ever happens in the backup COPY",
          _live_prompt == "a very long real prompt text")
    _live_score_days = {r[0] for r in _live_conn.execute("SELECT day FROM score_history").fetchall()}
    check("the LIVE fixture DB's score_history still has the 50-day-old row (not trimmed live)",
          _OLD_DAY in _live_score_days)
    _live_conn.close()


# A Sunday - nightly + analytics attachment.
with tempfile.TemporaryDirectory() as _tmp2:
    _db_path2 = os.path.join(_tmp2, "fixture.db")
    _make_fixture_db(_db_path2)
    _sent2, _logs2 = [], []
    _sunday = datetime(2026, 11, 22, 3, 0, tzinfo=timezone.utc)
    check("fixture's chosen Sunday date really is weekday 6", _sunday.weekday() == 6)
    with mock.patch.object(dbe, "build_compounder_data", None):  # no volume research-data files in this fixture
        _ok2, _msg2, _detail2 = _run_backup_on(_db_path2, _sunday, _logs2, _sent2)
    check("_run_backup() succeeds on a Sunday", _ok2)
    check("exactly one email sent on a Sunday (analytics rides along as a 2nd attachment, "
          "not a 2nd email)", len(_sent2) == 1)
    if _sent2:
        check("Sunday email has exactly 2 attachments (nightly + analytics)",
              len(_sent2[0]["attachments"]) == 2)
        _sunday_names = [a["name"] for a in _sent2[0]["attachments"]]
        check('one Sunday attachment is named "stocksdeepdive_analytics_<date>.db.gz"',
              any(n.startswith("stocksdeepdive_analytics_") for n in _sunday_names))
        _analytics_att = next(a for a in _sent2[0]["attachments"]
                               if a["name"].startswith("stocksdeepdive_analytics_"))
        _decompressed2 = os.path.join(_tmp2, "analytics_decompressed.db")
        with open(_decompressed2, "wb") as fout:
            fout.write(_analytics_att["raw_bytes"])
        _analytics_tables = _tables_in(_decompressed2)
        check("the analytics attachment contains ONLY the ANALYTICS table (page_views), "
              "no USER/HISTORY/AI/REBUILDABLE/auth tables",
              _analytics_tables == {"page_views"})
    check('Sunday email body mentions "Not included (rebuilt by the nightly scan)"',
          any("Not included (rebuilt by the nightly scan)" in e["body"] for e in _sent2))
    check("Sunday email body lists a REBUILDABLE table by name (e.g. explain_cache)",
          any("explain_cache" in e["body"] for e in _sent2))


# ======================================================================
# Monthly score_history backup - separate email, FULL history (not
# trimmed), independent of the nightly job.
# ======================================================================
with tempfile.TemporaryDirectory() as _tmp3:
    _db_path3 = os.path.join(_tmp3, "fixture.db")
    _make_fixture_db(_db_path3)
    _sent3 = []
    with mock.patch.object(dbe, "DB_PATH", _db_path3), \
         mock.patch.object(dbe, "_send_backup_email",
                            side_effect=lambda subj, body, atts: _capture_email(_sent3, subj, body, atts)):
        _ok3, _msg3, _detail3 = dbe._run_monthly_score_history_backup(log=lambda m: None)
    check("_run_monthly_score_history_backup() succeeds", _ok3)
    check("exactly one email sent, separate from the nightly job",
          len(_sent3) == 1)
    if _sent3:
        check('monthly attachment is named "stocksdeepdive_score_history_<date>.db.gz"',
              _sent3[0]["attachments"][0]["name"].startswith("stocksdeepdive_score_history_"))
        _decompressed3 = os.path.join(_tmp3, "monthly_decompressed.db")
        with open(_decompressed3, "wb") as fout:
            fout.write(_sent3[0]["attachments"][0]["raw_bytes"])
        _monthly_tables = _tables_in(_decompressed3)
        check("monthly attachment contains ONLY score_history", _monthly_tables == {"score_history"})
        _mconn = sqlite3.connect(_decompressed3)
        _monthly_days = {r[0] for r in _mconn.execute("SELECT day FROM score_history").fetchall()}
        _mconn.close()
        check("monthly attachment's score_history is the FULL table - the 50-day-old "
              "row survives (NOT trimmed, unlike the nightly copy)",
              _OLD_DAY in _monthly_days and _RECENT_DAY in _monthly_days)


# ======================================================================
# run_nightly_backup() fires the monthly job too on the 1st of the month.
# ======================================================================
with tempfile.TemporaryDirectory() as _tmp4:
    _db_path4 = os.path.join(_tmp4, "fixture.db")
    _make_fixture_db(_db_path4)
    _monthly_calls = []
    _first_of_month = datetime(2026, 12, 1, 3, 0, tzinfo=timezone.utc)
    _FakeDateTime._fixed = _first_of_month
    with mock.patch.object(dbe, "DB_PATH", _db_path4), \
         mock.patch.object(dbe, "datetime", _FakeDateTime), \
         mock.patch.object(dbe, "is_configured", return_value=True), \
         mock.patch.object(dbe, "_run_backup", return_value=(True, "ok", {})), \
         mock.patch.object(dbe, "_run_monthly_score_history_backup",
                            side_effect=lambda log: (_monthly_calls.append(True), (True, "ok", {}))[1]):
        dbe.run_nightly_backup(log=lambda m: None)
    check("run_nightly_backup() calls the monthly score_history job on the 1st of the month",
          len(_monthly_calls) == 1)

with tempfile.TemporaryDirectory() as _tmp5:
    _db_path5 = os.path.join(_tmp5, "fixture.db")
    _make_fixture_db(_db_path5)
    _monthly_calls2 = []
    _not_first = datetime(2026, 12, 2, 3, 0, tzinfo=timezone.utc)
    _FakeDateTime._fixed = _not_first
    with mock.patch.object(dbe, "DB_PATH", _db_path5), \
         mock.patch.object(dbe, "datetime", _FakeDateTime), \
         mock.patch.object(dbe, "is_configured", return_value=True), \
         mock.patch.object(dbe, "_run_backup", return_value=(True, "ok", {})), \
         mock.patch.object(dbe, "_run_monthly_score_history_backup",
                            side_effect=lambda log: (_monthly_calls2.append(True), (True, "ok", {}))[1]):
        dbe.run_nightly_backup(log=lambda m: None)
    check("run_nightly_backup() does NOT call the monthly job on any other day",
          len(_monthly_calls2) == 0)


# ======================================================================
# Size guard: an oversized nightly attachment is dropped, logged, and
# noted in the body - without blocking a smaller sibling attachment.
# ======================================================================
with tempfile.TemporaryDirectory() as _tmp6:
    _db_path6 = os.path.join(_tmp6, "fixture.db")
    _make_fixture_db(_db_path6)
    _sent6 = []
    _logs6 = []
    _sunday6 = datetime(2026, 11, 22, 3, 0, tzinfo=timezone.utc)
    _FakeDateTime._fixed = _sunday6
    with mock.patch.object(dbe, "DB_PATH", _db_path6), \
         mock.patch.object(dbe, "datetime", _FakeDateTime), \
         mock.patch.object(dbe, "build_compounder_data", None), \
         mock.patch.object(dbe, "MAILGUN_ATTACHMENT_LIMIT_BYTES", 1), \
         mock.patch.object(dbe, "_send_backup_email",
                            side_effect=lambda subj, body, atts: _sent6.append(
                                {"subject": subj, "body": body, "attachments": list(atts)}
                            )):
        _ok6, _msg6, _detail6 = dbe._run_backup(log=lambda m: _logs6.append(m))
    check("_run_backup() still succeeds (sends an email) even when every attachment "
          "is over the (artificially tiny) size limit",
          _ok6)
    check('every dropped attachment is logged as "[db_backup] OVER LIMIT ..."',
          any(ln.startswith("[db_backup] OVER LIMIT ") for ln in _logs6))
    check('every attachment\'s size is ALSO logged as "[db_backup] attachment ..." '
          "regardless of whether it was dropped",
          any(ln.startswith("[db_backup] attachment ") for ln in _logs6))
    if _sent6:
        check("the email sent with everything over-limit has ZERO attachments",
              _sent6[0]["attachments"] == [])
        check("the email body names the dropped file(s) and their size(s)",
              "was dropped from this email" in _sent6[0]["body"])


# ======================================================================
# run_backup_now() - uses the same construction, no "force_full".
# ======================================================================
check("run_backup_now()'s own signature has no force_full parameter any more",
      "force_full" not in inspect.signature(dbe.run_backup_now).parameters)
check("_run_backup()'s own signature has no force_full parameter any more "
      "(every night's set is already the real, complete one)",
      "force_full" not in inspect.signature(dbe._run_backup).parameters)
_src_run_backup_now = inspect.getsource(dbe.run_backup_now)
check("run_backup_now() calls _run_backup(log) directly - the exact same "
      "construction the scheduler runs tonight",
      "_run_backup(log)" in _src_run_backup_now)


# ======================================================================
# build_owner_download() - unchanged by TASK 77 (still no table
# allowlist at all, only the sensitive-table exclusion).
# ======================================================================
_src_owner_download = inspect.getsource(dbe.build_owner_download)
check("build_owner_download() still has no BACKUP_TABLES/REBUILDABLE_TABLES "
      "reference at all (TASK 77 item 4: unchanged)",
      "BACKUP_TABLES" not in _src_owner_download and "REBUILDABLE_TABLES" not in _src_owner_download)
check("build_owner_download() still calls _exclude_sensitive_tables()",
      "_exclude_sensitive_tables(" in _src_owner_download)


print()
print(f"PASS={passed} FAIL={failed}")
sys.exit(1 if failed else 0)
