"""
admin_metrics_store.py

Mega-batch Part 35.1: aggregate-only "site pulse" counters for the new
owner Admin Dashboard - a SEPARATE layer from metrics_store.py's existing
per-render page-view counter (that one is Streamlit-side, bumped from
app.py's own page renders; this one also covers server.py's HTTP-level
request counting, which sees traffic metrics_store never does - the very
first HTTP request of a browser session, before Streamlit's own websocket
takes over in-session navigation - see server.py's module docstring for
that proxy/websocket split).

HARD PRIVACY RULE (Part 35 instruction, verbatim): "count events, never
people - no IPs stored, no per-visitor identifiers, no browsing trails;
only aggregate daily counters." Concretely, in this module:
  - No IP address is ever stored anywhere in this file.
  - No raw email, name, or other visitor identifier is ever stored.
  - The one place this module comes close to an "identifier" is the
    daily-unique-signed-in-accounts count (the dashboard's "Sign-ins 87 /
    31 unique" tile) - answering that honestly needs SOME way to tell two
    sign-ins on the same day apart from the same account signing in
    twice. This is done with a SALTED, DAY-ROTATED hash
    (sha256(email|day|secret)) stored only in that day's own bucket:
    because the day is baked into the hash input, the SAME account gets
    a DIFFERENT hash every day, so two days' hash tables can never be
    joined to build a trail for one person - only "how many distinct
    hashes appeared today" is ever read back, and the table is pruned
    (prune_old_signin_hashes, called from the nightly volume-check job)
    well before any admin tile would read further back than 7 days.
  - Every other counter here is a plain (day, key) -> integer tally,
    exactly metrics_store.py's own pattern - see that module's docstring
    for why a per-day UPSERT keeps this table small forever rather than
    growing one row per event.

Same SQLite file / volume-resolution / WAL convention as every other
*_store.py in this codebase.
"""

import hashlib
import os
import sqlite3
from datetime import datetime, timedelta, timezone


def _data_dir():
    return os.environ.get("RAILWAY_VOLUME_MOUNT_PATH") or os.path.dirname(__file__)


DB_PATH = os.path.join(_data_dir(), "stocksdeepdive.db")

# Part 35 instruction, verbatim: counters are "kept 90 days (prune on
# write; tiny rows)", and the signed-in-account hash set is "pruned with
# the same 90-day retention". Applied here as a nightly sweep (see
# prune_old_counters/prune_old_signin_hashes, both called from
# scheduler_engine's volume-check job) rather than a delete on every
# write - a nightly sweep gives the exact same 90-day retention with no
# extra work on the hot request/sign-in path.
PRUNE_AFTER_DAYS = 90

# Bounds unbounded cardinality growth from a free-text `src=` query
# string: once a UTC day has already seen this many DISTINCT src tags,
# server.py's counting middleware folds any further never-seen-before tag
# that day into a fixed "other" bucket instead of creating a new row (see
# server.py's own _pulse_src_key - the cap is enforced there, in memory,
# before anything ever reaches this module, so it costs zero DB reads on
# the request path). 50 is generous headroom over the handful of real
# campaign tags this site has ever actually used (metrics_store's own
# admin "top src" table has never shown more than a dozen).
MAX_NEW_SRC_TAGS_PER_DAY = 50


def _conn():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS pulse_counters (
            day TEXT NOT NULL,
            key TEXT NOT NULL,
            count INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (day, key)
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS daily_signin_hashes (
            day TEXT NOT NULL,
            acct_hash TEXT NOT NULL,
            PRIMARY KEY (day, acct_hash)
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS job_status (
            job TEXT PRIMARY KEY,
            last_run_at TEXT NOT NULL,
            result TEXT NOT NULL,
            detail TEXT NOT NULL DEFAULT '',
            duration_seconds REAL
        )"""
    )
    return conn


def _today():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _day_n_ago(n):
    return (datetime.now(timezone.utc) - timedelta(days=n)).strftime("%Y-%m-%d")


def bump(key, n=1):
    """UPSERT count+n for today (UTC) under this key. Callers wrap this in
    try/except - a counting failure must never break the request/render
    it's measuring (same fail-open convention server.py's middleware
    itself follows - see that module's own docstring)."""
    if not key or not n:
        return
    day = _today()
    with _conn() as conn:
        conn.execute(
            """INSERT INTO pulse_counters (day, key, count) VALUES (?, ?, ?)
               ON CONFLICT(day, key) DO UPDATE SET count = count + excluded.count""",
            (day, key, int(n)),
        )


def bump_many(counts):
    """counts: {key: n}. One transaction for a batch flush (server.py's
    periodic in-memory-accumulator flush) instead of one commit per key -
    see server.py's _pulse_flush_once."""
    if not counts:
        return
    day = _today()
    with _conn() as conn:
        for key, n in counts.items():
            if not key or not n:
                continue
            conn.execute(
                """INSERT INTO pulse_counters (day, key, count) VALUES (?, ?, ?)
                   ON CONFLICT(day, key) DO UPDATE SET count = count + excluded.count""",
                (day, key, int(n)),
            )


def _acct_hash(email, day):
    # AUTH_COOKIE_SECRET is already a Railway-configured secret used
    # nowhere else this module can leak into - reusing it (rather than
    # inventing a second secret env var) keeps this to zero new required
    # config. Falls back to a fixed string in local dev only (no secret
    # configured there); production always has AUTH_COOKIE_SECRET set for
    # sign-in to work at all.
    secret = os.environ.get("AUTH_COOKIE_SECRET", "") or "sdd-pulse-fallback"
    raw = f"{email.strip().lower()}|{day}|{secret}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def record_signin(email):
    """One sign-in EVENT (bumps the 'signins' counter) plus this
    account's day-rotated hash (for the 'unique accounts' figure) - see
    module docstring for why the hash can never be linked across days or
    back to an email. Callers wrap this in try/except - see
    paywall_engine.restore_email_session(), the one call site, which
    already gates this behind its own once-per-session
    "_signup_recorded" flag, making this naturally rerun-safe (fires
    once per browser session, covering both the Google and email-code
    sign-in methods that flow through that same function)."""
    if not email:
        return
    day = _today()
    h = _acct_hash(email, day)
    with _conn() as conn:
        conn.execute(
            """INSERT INTO pulse_counters (day, key, count) VALUES (?, 'signins', 1)
               ON CONFLICT(day, key) DO UPDATE SET count = count + 1""",
            (day,),
        )
        conn.execute(
            "INSERT OR IGNORE INTO daily_signin_hashes (day, acct_hash) VALUES (?, ?)",
            (day, h),
        )


def prune_old_signin_hashes(log=print):
    """Deletes signed-in-account hash rows older than PRUNE_AFTER_DAYS
    (90, per the instruction) - called from scheduler_engine's nightly
    volume-check job, alongside its own retention pruning, so this table
    never grows unbounded and no hash outlives the window any admin tile
    actually reads. Never raises."""
    cutoff = _day_n_ago(PRUNE_AFTER_DAYS)
    try:
        with _conn() as conn:
            cur = conn.execute("DELETE FROM daily_signin_hashes WHERE day < ?", (cutoff,))
            n = max(cur.rowcount, 0)
        if n:
            log(f"[admin_metrics_store] pruned {n} old signed-in-account hash row(s)")
        return n
    except Exception as e:
        log(f"[admin_metrics_store] prune failed: {e}")
        return 0


def prune_old_counters(log=print):
    """Deletes pulse_counters rows older than PRUNE_AFTER_DAYS (90) -
    the instruction's own "kept 90 days (prune on write; tiny rows)"
    rule, applied as a nightly sweep (same call site as
    prune_old_signin_hashes above) rather than a delete on every write,
    which would add cost to the hot request/tool-open/sign-in paths for
    no benefit - the retention window is the same either way. Never
    raises."""
    cutoff = _day_n_ago(PRUNE_AFTER_DAYS)
    try:
        with _conn() as conn:
            cur = conn.execute("DELETE FROM pulse_counters WHERE day < ?", (cutoff,))
            n = max(cur.rowcount, 0)
        if n:
            log(f"[admin_metrics_store] pruned {n} old pulse-counter row(s)")
        return n
    except Exception as e:
        log(f"[admin_metrics_store] prune failed: {e}")
        return 0


def _sum_since(conn, key, since_day):
    row = conn.execute(
        "SELECT COALESCE(SUM(count), 0) FROM pulse_counters WHERE key = ? AND day >= ?",
        (key, since_day),
    ).fetchone()
    return row[0] if row else 0


def _sum_between_or_none(conn, key, start_day, end_day_exclusive):
    """Like _sum_since's COALESCE'd sum, but returns None (not 0) when
    NO ROW AT ALL falls in this window - the dashboard's own "—" delta
    rule ("deltas render '-' when no prior-week data exists yet") needs
    to tell "this counter genuinely recorded zero that week" apart from
    "this counter didn't exist yet that week" (e.g. the very first weeks
    after Part 35 ships, when server.py's request middleware simply
    wasn't running yet to record anything for the PRIOR window) - a
    plain COALESCE(SUM(...),0) can't distinguish those two cases."""
    row = conn.execute(
        "SELECT SUM(count) FROM pulse_counters WHERE key = ? AND day >= ? AND day < ?",
        (key, start_day, end_day_exclusive),
    ).fetchone()
    return row[0] if row and row[0] is not None else None


def pulse_7d():
    """Everything the Admin Dashboard's SITE PULSE row + tool-opens/src
    cards need, in one call: this-7d vs previous-7d for each headline
    counter, the 7-day unique-signed-in-accounts count, ranked tool
    opens, and ranked src tags. Never raises - returns zeroed figures on
    any read error, since this is a diagnostics page, not a load-bearing
    one."""
    since7 = _day_n_ago(6)   # today + previous 6 days = 7 days inclusive
    prev_start = _day_n_ago(13)
    prev_end = since7  # exclusive
    empty = {
        "requests": {"current": 0, "previous": 0},
        "requests_5xx": {"current": 0, "previous": 0},
        "signins": {"current": 0, "previous": 0},
        "signins_unique": 0,
        "bill_checks_energy": 0,
        "bill_checks_insurance": 0,
        "tool_opens": {},
        "src_tags": {},
    }
    try:
        out = {}
        with _conn() as conn:
            for key in ("requests", "requests_5xx", "signins"):
                cur = _sum_since(conn, key, since7)
                prev = _sum_between_or_none(conn, key, prev_start, prev_end)
                out[key] = {"current": cur, "previous": prev}
            uniq = conn.execute(
                "SELECT COUNT(DISTINCT acct_hash) FROM daily_signin_hashes WHERE day >= ?",
                (since7,),
            ).fetchone()
            out["signins_unique"] = uniq[0] if uniq else 0
            out["bill_checks_energy"] = _sum_since(conn, "bill_check_energy", since7)
            out["bill_checks_insurance"] = _sum_since(conn, "bill_check_insurance", since7)
            out["tool_opens"] = {}
            for k, v in conn.execute(
                "SELECT key, SUM(count) FROM pulse_counters WHERE key LIKE 'tool_open:%' "
                "AND day >= ? GROUP BY key ORDER BY 2 DESC",
                (since7,),
            ).fetchall():
                out["tool_opens"][k[len("tool_open:"):]] = v
            out["src_tags"] = {}
            for k, v in conn.execute(
                "SELECT key, SUM(count) FROM pulse_counters WHERE key LIKE 'src:%' "
                "AND day >= ? GROUP BY key ORDER BY 2 DESC LIMIT 10",
                (since7,),
            ).fetchall():
                out["src_tags"][k[len("src:"):]] = v
        return out
    except Exception:
        return empty


def record_job_result(job, result, detail="", duration_seconds=None):
    """result: 'ok' | 'warn' | 'error'. Called from scheduler_engine.py
    right after each nightly/weekly job finishes (see that module's
    _record_job wrapper). Callers wrap this in try/except - a metrics
    write must never take a real job down."""
    with _conn() as conn:
        conn.execute(
            """INSERT INTO job_status (job, last_run_at, result, detail, duration_seconds)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(job) DO UPDATE SET
                 last_run_at = excluded.last_run_at,
                 result = excluded.result,
                 detail = excluded.detail,
                 duration_seconds = excluded.duration_seconds""",
            (job, datetime.now(timezone.utc).isoformat(), result, detail or "",
             duration_seconds),
        )


def all_job_statuses():
    """[{'job','last_run_at','result','detail','duration_seconds'}, ...]
    ordered by job name - the Admin Dashboard's NIGHTLY JOBS table.
    Never raises - returns [] on any read error."""
    try:
        with _conn() as conn:
            rows = conn.execute(
                "SELECT job, last_run_at, result, detail, duration_seconds "
                "FROM job_status ORDER BY job"
            ).fetchall()
        return [
            {"job": r[0], "last_run_at": r[1], "result": r[2], "detail": r[3],
             "duration_seconds": r[4]}
            for r in rows
        ]
    except Exception:
        return []
