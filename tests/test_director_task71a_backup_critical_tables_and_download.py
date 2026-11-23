"""
TASK 71 Part A (Director-directed, owner-approved, 21 Nov 2026) - Backups.

CONTEXT: the nightly email backup is "critical tables only" on 6 of 7
nights (the full DB exceeds MAILGUN_ATTACHMENT_LIMIT_BYTES), and
CRITICAL_TABLES was missing compare_lists, compare_notes and
share_links (user-authored, stocksdeepdive.db) - those were only backed
up weekly.

UPDATE (TASK 77, Director-directed, owner-approved, 23 Nov 2026):
CRITICAL_TABLES/_trim_to_critical_tables() were replaced by an explicit
BACKUP_TABLES allowlist (groups: USER/HISTORY/AI/ANALYTICS) and the
generic _trim_to_tables(db_path, keep_tables) - the checks below that
exercised the OLD names now exercise the new ones (BACKUP_TABLES["USER"]
is the direct successor to CRITICAL_TABLES; _trim_to_tables(path,
BACKUP_TABLES["USER"]) is the direct successor to
_trim_to_critical_tables(path)). See tests/test_director_task77_
backup_tables_drift.py for the full TASK 77 coverage (BACKUP_TABLES'
other 3 groups, REBUILDABLE_TABLES, the drift test, the nightly/Sunday/
monthly email construction, the size guard). Everything else in this
file (build_owner_download(), the job-lock checks, the owner-gate
check) is UNCHANGED by TASK 77 and still covers the real, current code.

PART A (this file covers all of it):
  1. compare_lists/compare_notes/share_links added to CRITICAL_TABLES
     (now BACKUP_TABLES["USER"] - see UPDATE above).
     EXCLUDED_FROM_EMAIL_TABLES (the 4 auth tables) unchanged.
  2. db_backup_engine.build_owner_download() - the Admin dashboard's new
     owner-only "Download full backup" button (app.py, inside the same
     ADMIN_REFRESH_KEY-gated _render_compounder_admin_panel() function
     as the existing "Backup now" button - same owner gate, no new
     gate introduced). Builds the same sanitised copy the email path
     uses (VACUUM INTO -> _exclude_sensitive_tables() -> gzip), no size
     limit, no critical-tables trim - the owner always gets everything
     except the auth tables. Returns the gzip bytes in memory (never
     written to a web-served path) and deletes every temp file it
     creates before returning. Refuses while the nightly backup job's
     own lock is held (scheduler_engine.backup_job_lock_active(), a new
     read-only/non-blocking check mirroring nightly_scan_window_
     active()'s existing contract, against the "backup" job lock
     _run_backup() already acquires via _acquire_job_lock("backup",
     log)).

Covers:
  - CRITICAL_TABLES includes the three new tables; EXCLUDED_FROM_EMAIL_
    TABLES is byte-identical to before this task (still exactly the 4
    auth tables, nothing added or removed).
  - A fixture DB's critical-tables trim (_trim_to_critical_tables(), the
    same function the nightly critical-fallback path uses) keeps the
    three new tables and every pre-existing critical table, drops a
    non-critical table, and - since _exclude_sensitive_tables() always
    runs first in the real pipeline - the auth tables are gone too.
  - build_owner_download(): produces a valid gzip'd SQLite copy with
    every non-auth table intact (including the three new critical
    tables AND a table that's neither critical nor excluded - no size-
    based or critical-tables-based trimming on this path, only the
    sensitive-table exclusion) and the auth tables gone; the returned
    data is the actual file bytes (decompresses to a valid SQLite
    database openable by sqlite3); no temp directory is left behind
    after it returns, success or failure.
  - Refuses (ok=False, result=None) while scheduler_engine.backup_job_
    lock_active() reports the "backup" job lock held with a fresh
    heartbeat; proceeds normally once that heartbeat goes stale/the
    lock is released.
  - scheduler_engine.backup_job_lock_active() itself: True right after
    _acquire_job_lock("backup", ...) succeeds, False after _release_
    job_lock("backup") runs, False when the lock file's heartbeat is
    stale, False when no lock file exists at all - the same read-only
    contract nightly_scan_window_active() already has, just against a
    different job name.
  - The new "Download full backup" button's code lives inside the same
    ADMIN_REFRESH_KEY-gated function as the pre-existing "Backup now"
    button (owner-only, no separate/weaker gate introduced) - confirmed
    via source inspection.

Full suite, 0 failures required alongside this file.

Run: python3 tests/test_director_task71a_backup_critical_tables_and_download.py
"""
import gzip
import os
import sqlite3
import sys
import tempfile
import time
import uuid
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
import scheduler_engine


# ======================================================================
# CHECK 1-2: BACKUP_TABLES["USER"] (TASK 77's successor to
# CRITICAL_TABLES)/EXCLUDED_FROM_EMAIL_TABLES.
# ======================================================================
check("BACKUP_TABLES['USER'] includes compare_lists, compare_notes, share_links",
      all(t in dbe.BACKUP_TABLES["USER"] for t in ("compare_lists", "compare_notes", "share_links")))
check("EXCLUDED_FROM_EMAIL_TABLES is unchanged - exactly the 4 auth tables, nothing new",
      set(dbe.EXCLUDED_FROM_EMAIL_TABLES) == {
          "auth_sessions", "auth_codes", "auth_ip_sends", "daily_signin_hashes",
      })


def _make_fixture_db(path):
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE compare_lists (id INTEGER PRIMARY KEY, data TEXT)")
    conn.execute("INSERT INTO compare_lists VALUES (1, 'list-a')")
    conn.execute("CREATE TABLE compare_notes (id INTEGER PRIMARY KEY, note TEXT)")
    conn.execute("INSERT INTO compare_notes VALUES (1, 'note-a')")
    conn.execute("CREATE TABLE share_links (id INTEGER PRIMARY KEY, token TEXT)")
    conn.execute("INSERT INTO share_links VALUES (1, 'tok-a')")
    conn.execute("CREATE TABLE signups (email TEXT)")
    conn.execute("INSERT INTO signups VALUES ('a@example.com')")
    # a REBUILDABLE table (not in BACKUP_TABLES["USER"], not EXCLUDED either)
    conn.execute("CREATE TABLE scan_cache (ticker TEXT)")
    conn.execute("INSERT INTO scan_cache VALUES ('AAPL')")
    # the 4 sensitive/auth tables
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


# ======================================================================
# CHECK 3-5: _trim_to_tables(path, BACKUP_TABLES["USER"]) - TASK 77's
# generic successor to _trim_to_critical_tables() - keeps the 3 new
# tables + a pre-existing USER table, drops a non-USER table; combined
# with the sensitive-table exclusion the real pipeline always runs
# first, the auth tables are gone too.
# ======================================================================
with tempfile.TemporaryDirectory() as _tmp:
    _db_path = os.path.join(_tmp, "fixture.db")
    _make_fixture_db(_db_path)
    dbe._exclude_sensitive_tables(_db_path)
    dbe._trim_to_tables(_db_path, dbe.BACKUP_TABLES["USER"])
    _remaining = _tables_in(_db_path)
    check("USER-tables trim keeps compare_lists/compare_notes/share_links",
          {"compare_lists", "compare_notes", "share_links"}.issubset(_remaining))
    check("USER-tables trim keeps a pre-existing USER table (signups) too",
          "signups" in _remaining)
    check("USER-tables trim drops the non-USER scan_cache table, and the "
          "auth tables are gone (sensitive-exclusion ran first, same as the real pipeline)",
          "scan_cache" not in _remaining
          and not _remaining & {"auth_sessions", "auth_codes", "auth_ip_sends", "daily_signin_hashes"})


# ======================================================================
# CHECK 6-10: build_owner_download() end-to-end against a fixture DB -
# full copy (no critical-tables trim), auth tables gone, valid gzip'd
# SQLite, no leftover temp directory.
# ======================================================================
with tempfile.TemporaryDirectory() as _tmp2:
    _db_path2 = os.path.join(_tmp2, "stocksdeepdive.db")
    _make_fixture_db(_db_path2)
    _tmp_dirs_before = set(os.listdir(tempfile.gettempdir()))
    with mock.patch.object(dbe, "DB_PATH", _db_path2), \
         mock.patch.object(scheduler_engine, "backup_job_lock_active", return_value=False):
        _ok, _msg, _result = dbe.build_owner_download()
    check("build_owner_download() succeeds for a fixture DB with no backup job running",
          _ok and _result is not None)
    check('build_owner_download() filename is "stocksdeepdive_full_<UTC date>.db.gz"',
          _result["filename"].startswith("stocksdeepdive_full_") and _result["filename"].endswith(".db.gz"))
    check("build_owner_download()'s own size_bytes matches the actual returned data length",
          _result["size_bytes"] == len(_result["data"]))

    _decompressed = gzip.decompress(_result["data"])
    _out_path = os.path.join(_tmp2, "downloaded.db")
    with open(_out_path, "wb") as f:
        f.write(_decompressed)
    _downloaded_tables = _tables_in(_out_path)
    check("the downloaded copy is a VALID SQLite database (openable, real tables readable)",
          len(_downloaded_tables) > 0)
    check("the downloaded copy keeps EVERY non-auth table (compare_lists/compare_notes/"
          "share_links/signups AND the non-critical scan_cache - full copy, no critical-"
          "tables trim on this path) and drops all 4 auth tables",
          {"compare_lists", "compare_notes", "share_links", "signups", "scan_cache"}.issubset(_downloaded_tables)
          and not _downloaded_tables & {"auth_sessions", "auth_codes", "auth_ip_sends", "daily_signin_hashes"})
    _conn = sqlite3.connect(_out_path)
    _row = _conn.execute("SELECT data FROM compare_lists WHERE id=1").fetchone()
    _conn.close()
    check("a real row's data survives the round trip (compare_lists id=1 -> 'list-a')",
          _row == ("list-a",))

    _tmp_dirs_after = set(os.listdir(tempfile.gettempdir()))
    _new_dirs = _tmp_dirs_after - _tmp_dirs_before
    check("no sdd_owner_backup_* temp directory is left behind after a successful build",
          not any(d.startswith("sdd_owner_backup_") for d in _new_dirs))


# ======================================================================
# CHECK 11-12: a failed build (VACUUM INTO can't even open the source
# DB - its parent directory doesn't exist) still cleans up its own temp
# directory and returns (False, message, None). (sqlite3.connect() on a
# path whose parent dir IS present silently creates a fresh empty DB
# rather than raising - not a real failure case, so this uses a path
# sqlite3 genuinely can't open at all.)
# ======================================================================
with tempfile.TemporaryDirectory() as _tmp3:
    _unopenable_db_path = os.path.join(_tmp3, "no_such_subdir", "stocksdeepdive.db")
    _tmp_dirs_before2 = set(os.listdir(tempfile.gettempdir()))
    with mock.patch.object(dbe, "DB_PATH", _unopenable_db_path), \
         mock.patch.object(scheduler_engine, "backup_job_lock_active", return_value=False):
        _ok2, _msg2, _result2 = dbe.build_owner_download()
    check("build_owner_download() fails cleanly (ok=False, result=None) when the source DB "
          "genuinely can't be opened",
          not _ok2 and _result2 is None and _msg2)
    _tmp_dirs_after2 = set(os.listdir(tempfile.gettempdir()))
    check("a FAILED build still leaves no sdd_owner_backup_* temp directory behind",
          not any(d.startswith("sdd_owner_backup_") for d in (_tmp_dirs_after2 - _tmp_dirs_before2)))


# ======================================================================
# CHECK 13: refuses while the nightly backup job's own lock is held.
# ======================================================================
with tempfile.TemporaryDirectory() as _tmp4:
    _db_path4 = os.path.join(_tmp4, "stocksdeepdive.db")
    _make_fixture_db(_db_path4)
    with mock.patch.object(dbe, "DB_PATH", _db_path4), \
         mock.patch.object(scheduler_engine, "backup_job_lock_active", return_value=True):
        _ok3, _msg3, _result3 = dbe.build_owner_download()
    check("build_owner_download() refuses (ok=False, result=None, an explanatory message) "
          "while the nightly backup job's lock is held",
          not _ok3 and _result3 is None and "running right now" in _msg3)


# ======================================================================
# CHECK 14-17: scheduler_engine.backup_job_lock_active() itself - the
# new read-only check, against a REAL lock (acquired/released via the
# same _acquire_job_lock/_release_job_lock the nightly job uses).
# ======================================================================
with tempfile.TemporaryDirectory() as _tmp5:
    with mock.patch.dict(os.environ, {"RAILWAY_VOLUME_MOUNT_PATH": _tmp5}):
        check("backup_job_lock_active() is False with no lock file at all",
              scheduler_engine.backup_job_lock_active() is False)

        _acquired = scheduler_engine._acquire_job_lock("backup", log=lambda *a, **k: None)
        check('backup_job_lock_active() is True right after _acquire_job_lock("backup", ...) succeeds',
              _acquired and scheduler_engine.backup_job_lock_active() is True)

        scheduler_engine._release_job_lock("backup")
        check('backup_job_lock_active() is False after _release_job_lock("backup")',
              scheduler_engine.backup_job_lock_active() is False)

        # A stale heartbeat (older than _JOB_LOCK_HEARTBEAT_STALE_SECONDS)
        # reads as not-active, even though the lock file still exists.
        _lock_path = scheduler_engine._lock_path("backup")
        import json as _json
        with open(_lock_path, "w") as f:
            _json.dump({
                "pid": 1, "boot_id": uuid.uuid4().hex,
                "started": time.time() - 3600,
                "heartbeat": time.time() - scheduler_engine._JOB_LOCK_HEARTBEAT_STALE_SECONDS - 5,
            }, f)
        check("backup_job_lock_active() is False once the lock file's own heartbeat is stale",
              scheduler_engine.backup_job_lock_active() is False)


# ======================================================================
# CHECK 18: the new button's code lives inside the SAME ADMIN_REFRESH_
# KEY-gated function as the pre-existing "Backup now" button - no new,
# separate (and potentially weaker) gate was introduced for it.
# ======================================================================
import ast

_repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_app_src = open(os.path.join(_repo_root, "app.py")).read()
_app_tree = ast.parse(_app_src)
_panel_fn = next(
    n for n in ast.walk(_app_tree)
    if isinstance(n, ast.FunctionDef) and n.name == "_render_compounder_admin_panel"
)
_panel_src = ast.get_source_segment(_app_src, _panel_fn)
check('_render_compounder_admin_panel() still returns early when ADMIN_REFRESH_KEY is unset '
      "(the owner gate both buttons rely on)",
      "if not admin_key:\n        return" in _panel_src or "if not admin_key:\n            return" in _panel_src
      or "if not admin_key:" in _panel_src)
check('the new "Prepare full backup for download"/"Download full backup" controls are '
      "defined INSIDE _render_compounder_admin_panel() - same owner gate as \"Backup now\", "
      "no separate gate introduced",
      "cp_admin_owner_backup_prepare" in _panel_src and "cp_admin_owner_backup_download" in _panel_src
      and "cp_admin_backup_now" in _panel_src)


print()
print(f"PASS={passed} FAIL={failed}")
sys.exit(1 if failed else 0)
