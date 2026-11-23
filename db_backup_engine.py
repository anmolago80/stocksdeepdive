"""
db_backup_engine.py

Mega-batch Part 10: off-site database backup - the real disaster
protection. stocksdeepdive.db (every account, portfolio, watchlist,
alert, checklist and usage record) currently has NO backup anywhere;
this module makes one nightly, over the site's existing Mailgun HTTP
API path (see email_auth.py's own docstring for why Mailgun over SMTP
- Railway blocks outbound SMTP), to the owner's own inbox
(ai_gate.owner_email()) - never any third-party storage service.

CONSISTENT COPY, NOT A RAW FILE COPY: SQLite's `VACUUM INTO` is used
to make the copy (never a plain file copy of a live DB, which could
capture a half-written page mid-write while the app's own connections
have it open - SQLite's docs call this exact hazard out explicitly).
VACUUM INTO also compacts the copy as a side effect, which only helps
here.

TASK 77 (Director-directed, owner-approved, 23 Nov 2026 - TASK 71 Part
B): replaces the old CRITICAL_TABLES-fallback + weekly-whole-database
design with an explicit allowlist, BACKUP_TABLES, measured against the
owner's own full download (23 Nov 2026, 47.6MB gz): the old 6-of-7-
nights "critical tables only" email was 0.03MB (far smaller than it
needed to be - whole categories of real, irreplaceable or genuinely
useful data were being dropped every night except the Sunday full
send, which itself had grown past Mailgun's 25MB limit).

BACKUP_TABLES is a dict of 4 groups:
  "USER"      - every account/portfolio/watchlist/alert/checklist/
                scenario/ledger/screen/research table - irreplaceable,
                user- or owner-authored. Sent every night.
  "HISTORY"   - score/snapshot/pool/health-run history - real to lose
                (it's the site's own track record), not re-fetchable
                from a live rescan the way a cache is. Sent every
                night, with score_history trimmed to the last
                SCORE_HISTORY_RETENTION_DAYS days (the FULL history
                goes out separately, once a month - see below).
  "AI"        - top100_scores, with its "prompt" column NULLed in the
                backup COPY only (never the live DB) - the prompt text
                is large and reconstructable from the model/engine
                version on file; raw_response (the actual AI output)
                and every other column are kept. Sent every night.
  "ANALYTICS" - page-view/visitor-hash/signin-count aggregates - real,
                but lower-stakes than USER/HISTORY/AI and the biggest
                single contributor to the old full backup's size.
                Sent ONCE A WEEK, Sunday (UTC), as a SEPARATE
                attachment on the same email.
Every table not in one of those four groups, EXCLUDED_FROM_EMAIL_
TABLES (below, unchanged), or REBUILDABLE_TABLES (an explicit list of
caches/fetch-logs/scan-derived state this site's own nightly scan
rebuilds on its own - tests/test_director_task77_backup_tables_
drift.py's own drift test FAILS the build if a table ever exists in
none of the three) is, by construction, impossible - every table this
codebase's *_store.py modules create is accounted for somewhere.

NIGHTLY EMAIL: one attachment, stocksdeepdive_backup_<date>.db.gz =
USER + HISTORY + AI (score_history trimmed to the last 35 days).
SUNDAY (UTC) adds a second attachment, stocksdeepdive_analytics_
<date>.db.gz = ANALYTICS, plus the pre-existing weekly extras
(compounder_data.json/company_potential_corrections.json straight from
the volume, if present).
1ST OF THE MONTH (UTC): a separate, independent email carries ONLY the
FULL (untrimmed) score_history table, stocksdeepdive_score_history_
<date>.db.gz - the one place the full history is protected, since the
nightly copy only ever carries the last 35 days.
There is no more weekly whole-database email, and no more "critical
tables only" fallback night - every night sends the SAME explicit set,
deterministically, regardless of size.

SIZE GUARD: every attachment's gzipped size is logged every night
("[db_backup] attachment <file> <bytes>"), whether or not it's sent.
An attachment over MAILGUN_ATTACHMENT_LIMIT_BYTES is NEVER sent
silently - it's dropped from that night's email, the email body says
which file was dropped and its size, and "[db_backup] OVER LIMIT <file>
<bytes>" is logged. The other attachment(s) on that email, if any, are
unaffected - e.g. an over-limit analytics file on a Sunday still lets
the main nightly file go out.

FAILURE HANDLING: any exception anywhere in the nightly job is caught,
logged, and emailed to the owner via the SAME Mailgun path AT MOST
ONCE per distinct failure streak (see _state()'s
"last_failure_streak_notified" flag - cleared the moment a run next
succeeds) - never a nightly spam stream for one persistent problem.
The monthly score_history email is best-effort and tracked separately
(its own failure never blocks or masks the nightly job's own state).

ADMIN PANEL: last_backup_status() and run_backup_now() are the two
public entry points app.py's admin panel calls for the "Backup now"
button and the "last backup sent: <date> [check/warn]" status line -
a loud warning once the last SUCCESSFUL backup is more than
STALE_WARNING_DAYS old. "Backup now" runs the exact same construction
the scheduler would run tonight (nightly set, plus the Sunday/1st-of-
month extras if today happens to be one) - there's no more separate
"forced full" mode to ask for, since every night's set is already the
real, complete one. build_owner_download() (TASK 71 Part A, 21 Nov
2026) is a third entry point: the same panel's owner-only "Download
full backup" button - a full, sanitised copy with NO table allowlist
at all (every table except EXCLUDED_FROM_EMAIL_TABLES), served
in-memory via st.download_button, never written to a web-served path -
UNCHANGED by TASK 77.

PRIVACY: the backup is emailed ONLY to ai_gate.owner_email() - this
site's own owner inbox - via Mailgun, exactly like every other
outbound email this site already sends. Never written to, or
transmitted via, any third-party storage service; nothing here adds a
new data-sharing surface, so no /privacy update is needed.

RESTORE (TASK 77 item 6): to rebuild a database from these emails -
  1. Take the MOST RECENT nightly stocksdeepdive_backup_<date>.db.gz.
     gunzip it and drop it in as stocksdeepdive.db on a fresh volume.
  2. If a stocksdeepdive_score_history_<date>.db.gz exists from a
     later 1st-of-the-month than the nightly file's own date, gunzip
     it and ATTACH its score_history table into the restored DB,
     replacing the (35-day-trimmed) one the nightly file carried -
     this recovers the FULL history rather than just the last 35 days.
  3. If a stocksdeepdive_analytics_<date>.db.gz exists from the most
     recent Sunday, gunzip it and ATTACH its tables (page_views/
     daily_visitor_hashes/daily_human_hashes/visitor_hashes_30d/
     owner_visitor_hashes_30d/pulse_counters/signin_accounts) into the
     restored DB the same way - optional, since these are lower-stakes
     analytics, not account state.
  4. Restart the service. Every REBUILDABLE_TABLES table (caches,
     fetch logs, scan-derived state) and every EXCLUDED_FROM_EMAIL_
     TABLES table (auth sessions/codes) recreate themselves empty via
     each *_store.py module's own CREATE TABLE IF NOT EXISTS the
     moment the restored app first touches them - callers sign in
     again (exactly as if their session had simply expired) and the
     next nightly scan refills every cache/history table from scratch.
     Nothing breaks; nothing needs to be rebuilt by hand.
"""

import gzip
import json
import os
import shutil
import sqlite3
import tempfile
from datetime import datetime, timezone, timedelta

import requests

import ai_gate

try:
    import build_compounder_data
except Exception:  # pragma: no cover - never let an import hiccup break backups
    build_compounder_data = None


def _data_dir():
    return os.environ.get("RAILWAY_VOLUME_MOUNT_PATH") or os.path.dirname(__file__)


DB_PATH = os.path.join(_data_dir(), "stocksdeepdive.db")
STATE_PATH = os.path.join(_data_dir(), "db_backup_state.json")

# Mailgun's documented per-message limit is 25MB; kept a conservative
# margin under that for MIME/multipart overhead and a second attachment
# on a Sunday send.
MAILGUN_ATTACHMENT_LIMIT_BYTES = 20 * 1024 * 1024
STALE_WARNING_DAYS = 3
ANALYTICS_WEEKDAY = 6  # UTC Sunday - its own slot, doesn't collide with
                        # the digest (also Sunday, later hour) or the
                        # earnings refresh (Wednesday). Was named
                        # WEEKLY_FULL_WEEKDAY before TASK 77 - renamed
                        # since there's no more "weekly full" concept,
                        # only the weekly ANALYTICS attachment.
SCORE_HISTORY_RETENTION_DAYS = 35

# TASK 77 (Director-directed, owner-approved, 23 Nov 2026) - see this
# module's own docstring for the full "why" and the nightly/Sunday/
# monthly email shape. Every table in this codebase's *_store.py
# modules is in exactly one of BACKUP_TABLES's 4 groups,
# EXCLUDED_FROM_EMAIL_TABLES, or REBUILDABLE_TABLES below - enforced by
# tests/test_director_task77_backup_tables_drift.py's own drift test,
# which builds the REAL schema (every module's own CREATE TABLE IF NOT
# EXISTS) and fails if a table is missing from all three.
BACKUP_TABLES = {
    # Every account/portfolio/watchlist/alert/checklist/scenario/
    # ledger/screen/research table - irreplaceable, user- or owner-
    # authored. Sent every night, in full, no trim.
    "USER": [
        # Pre-TASK-77 CRITICAL_TABLES - unchanged bucket, just renamed.
        "signups",
        "portfolios", "portfolio_holdings", "portfolio_settings",
        "iv_overrides", "portfolio_seed_log",
        "watchlist", "alerts", "alert_hits_pending", "alert_eval_log",
        "checklists", "ai_usage", "ai_settings", "push_subscriptions",
        "card_blurbs", "author_positions",
        "blog_posts", "blog_comments", "blog_redirects", "followers",
        "newsletter_subscribers",
        "compare_lists", "compare_notes", "share_links",
        # TASK 77 additions - confirmed user/owner-authored from each
        # table's own *_store.py module (not a cache/log/derived
        # state): rebalance_mixes/budget_plans*/super_scenarios*/
        # property_vs_index_scenarios*/debt_recycling_scenarios*
        # (portfolio_store.py/tools_store.py - a user's own saved
        # what-if scenarios, same bucket as iv_overrides/watchlist
        # above); screen_imports/screen_import_tickers (tools_store.py
        # - a user's own uploaded screener results); research_status/
        # custom_categories (card_blurb_store.py/tools_store.py - the
        # OWNER's own research-workflow and categorisation state, not
        # re-derivable from a rescan); ledger_rules/ledger_imports/
        # ledger_entries/ledger_category_flags (tools_store.py - a
        # user's own imported transaction ledger and categorisation
        # rules); account_currency (tools_store.py - a user's own
        # display-currency choice); bill_checks/bill_check_history
        # (bill_check_engine.py - a user's own saved fuel/bill checks;
        # bill_check_usage, a trial/rate-limit COUNTER rather than
        # saved content, is deliberately left in REBUILDABLE_TABLES
        # instead - losing it just resets a monthly counter, not a
        # user's own data); typical_deal_rates (bill_check_engine.py -
        # the OWNER's own reference rate table, hand-maintained, not
        # fetched); portfolio_watchdog_state (portfolio_watchdog_
        # engine.py - a user's own per-portfolio alert/watch state).
        "rebalance_mixes",
        "budget_plans", "budget_plan_names",
        "super_scenarios", "super_scenario_names",
        "property_vs_index_scenarios", "property_vs_index_scenario_names",
        "debt_recycling_scenarios", "debt_recycling_scenario_names",
        "screen_imports", "screen_import_tickers",
        "research_status", "custom_categories",
        "ledger_rules", "ledger_imports", "ledger_entries", "ledger_category_flags",
        "account_currency",
        "bill_checks", "bill_check_history",
        "typical_deal_rates",
        "portfolio_watchdog_state",
    ],
    # Score/snapshot/pool/health-run HISTORY - the site's own track
    # record, real to lose, not re-fetchable the way a cache is. Sent
    # every night; score_history is trimmed to the last
    # SCORE_HISTORY_RETENTION_DAYS days here - the FULL table goes out
    # separately, once a month (see _run_monthly_score_history_
    # backup() below).
    "HISTORY": [
        "score_history",
        "quote_snapshots", "quote_snapshot_runs",
        "top100_qos_pool", "pool_presence", "top100_pool",
        "top100_daily_submissions",
        "portfolio_health_runs",
        "results_events",
    ],
    # The AI engine's own scored output - top100_scores. "prompt" (the
    # large input text sent to the model) is NULLed in the BACKUP COPY
    # ONLY by _null_column_values() below - never the live DB - since
    # it's reconstructable from the engine/model version on file;
    # raw_response (the model's actual output) and every other column
    # are kept whole.
    "AI": ["top100_scores"],
    # Page-view/visitor-hash/signin-count aggregates - real, but lower-
    # stakes than USER/HISTORY/AI, and the single biggest contributor
    # to the old full backup's size. Sent ONCE A WEEK, Sunday (UTC), as
    # a separate attachment.
    "ANALYTICS": [
        "page_views",
        "daily_visitor_hashes", "daily_human_hashes",
        "visitor_hashes_30d", "owner_visitor_hashes_30d",
        "pulse_counters",
        "signin_accounts",
    ],
}

# Every table NOT in a BACKUP_TABLES group, EXCLUDED_FROM_EMAIL_TABLES
# or caught by the drift test (see BACKUP_TABLES's own comment) - a
# cache, a fetch log, or scan/batch-run-derived state this site's own
# nightly scan rebuilds on its own over time. Never emailed; the owner
# can always rebuild one of these from a fresh re-scan/re-fetch, which
# is exactly what the next nightly scan does automatically after a
# restore (see this module's own RESTORE section above).
REBUILDABLE_TABLES = [
    "aer_plan_cache",
    "bill_check_usage",
    "buyback_summary",
    "comment_triage",
    "earnings_watch",
    "etf_fund_facts",
    "explain_cache", "explain_cache_meta",
    "health_policy_dataset_cache",
    "insider_fetch_log", "insider_filings",
    "job_status",
    "news_events", "news_fetch_log",
    "newsletter_ip_sends", "newsletter_sends",
    "quote_rejections",
    "sector_cache",
    "snapshots",
    "stress_long_history_v4", "stress_result_cache_v10",
    "top100_batch_state", "top100_ingest_log", "top100_oneoff_queue",
    "top100_prescore_state", "top100_score_failures", "top100_submitting_marker",
]

# Commit K (21 Sep 2026, owner-reported): the emailed backup used to
# carry the full DB unencrypted, including live session tokens
# (auth_sessions) and pending sign-in codes (auth_codes) - anyone who
# could read that email (a compromised inbox, a misdirected forward, a
# Mailgun-side leak) could impersonate every currently-signed-in user
# without ever needing a password. Restore needs none of these four - a
# session token/sign-in code is only ever useful for the few minutes/
# days it's genuinely live, and a user who's lost theirs simply signs in
# again (email_auth.py's own _conn() recreates all four as empty tables
# on next startup via its usual CREATE TABLE IF NOT EXISTS, the same
# "guarded, in-place migration" convention every *_store.py module here
# already uses - a restore from the emailed copy signs every account out
# and forces a fresh sign-in code, which is the intended, safe outcome,
# not a bug). auth_ip_sends/daily_signin_hashes are rate-limit/analytics
# bookkeeping, not account state - same reasoning, lower stakes.
#
# auth_codes/auth_sessions are deliberately never in BACKUP_TABLES -
# _exclude_sensitive_tables() below runs BEFORE every other trim, on
# EVERY send path (nightly, Sunday-analytics, monthly score_history,
# and build_owner_download() alike) - not a size decision made only
# some nights, a privacy one made unconditionally, every time.
EXCLUDED_FROM_EMAIL_TABLES = [
    "auth_sessions", "auth_codes", "auth_ip_sends", "daily_signin_hashes",
]


# -----------------------------------------------------------------
# Small state file - same _load_state/_save_state convention as
# scheduler_engine.py (atomic write via a .tmp + os.replace).
# -----------------------------------------------------------------

def _load_state():
    try:
        with open(STATE_PATH) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save_state(state):
    try:
        tmp = STATE_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(state, f)
        os.replace(tmp, STATE_PATH)
    except OSError:
        pass


# -----------------------------------------------------------------
# The consistent copy + table-allowlist trim.
# -----------------------------------------------------------------

def _vacuum_into(dest_path):
    """A consistent snapshot of the live DB via SQLite's own backup
    mechanism - see module docstring for why this, not a raw file
    copy. Raises on failure; the caller decides what to do about it."""
    if os.path.exists(dest_path):
        os.remove(dest_path)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    try:
        conn.execute("VACUUM INTO ?", (dest_path,))
    finally:
        conn.close()


def _trim_to_tables(db_path, keep_tables):
    """Opens the (already-copied, so this never touches the live DB)
    `db_path` and drops every table NOT in `keep_tables`, then VACUUMs
    it to actually shrink the file. TASK 77: generalised from the old
    _trim_to_critical_tables() (which only ever took the hardcoded
    CRITICAL_TABLES list) so the SAME function builds every email's
    attachment - nightly (USER+HISTORY+AI), Sunday analytics
    (ANALYTICS), and the monthly score_history-only copy (just
    ["score_history"]) - from one mechanism, not three near-duplicates."""
    conn = sqlite3.connect(db_path, timeout=30)
    try:
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()]
        keep = set(keep_tables)
        for t in tables:
            if t not in keep:
                conn.execute(f'DROP TABLE IF EXISTS "{t}"')
        conn.commit()
        conn.execute("VACUUM")
    finally:
        conn.close()


def _exclude_sensitive_tables(db_path):
    """Opens the (already-copied, so this never touches the live DB)
    `db_path` and unconditionally drops every table in EXCLUDED_FROM_
    EMAIL_TABLES, then VACUUMs it to actually shrink the file - same
    DROP-and-VACUUM mechanics as _trim_to_tables() above, but an
    EXCLUSION list applied on EVERY send path (that one is an
    INCLUSION list, applied per email type - see each call site).
    Called first, right after the VACUUM INTO copy is made, so every
    later branch inherits the exclusion automatically; nothing
    downstream has to remember to apply it a second time. Raises on
    failure, same convention as _vacuum_into() - the caller fails the
    WHOLE backup rather than risk sending an un-trimmed copy if this
    step itself can't be trusted to have worked."""
    conn = sqlite3.connect(db_path, timeout=30)
    try:
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()]
        for t in tables:
            if t in EXCLUDED_FROM_EMAIL_TABLES:
                conn.execute(f'DROP TABLE IF EXISTS "{t}"')
        conn.commit()
        conn.execute("VACUUM")
    finally:
        conn.close()


def _null_column_values(db_path, table, column):
    """Sets every row's `column` to NULL in the (already-copied) db at
    `db_path`, leaving every other column and every other table
    untouched - used for top100_scores.prompt (TASK 77): the backup
    COPY loses the (large, reconstructable) prompt text; the live DB
    is never touched by this function at all. A no-op (never raises)
    if the table doesn't exist in this copy - callers that already
    trimmed to a table list not including `table` can call this
    unconditionally without checking first."""
    conn = sqlite3.connect(db_path, timeout=30)
    try:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        if exists:
            conn.execute(f'UPDATE "{table}" SET "{column}" = NULL')
            conn.commit()
            conn.execute("VACUUM")
    finally:
        conn.close()


def _trim_score_history_days(db_path, days):
    """Deletes every score_history row older than `days` days ago (UTC
    "today", inclusive) from the (already-copied) db at `db_path` -
    the nightly/Sunday backup only ever carries the recent window; the
    FULL table is protected separately, once a month (see
    _run_monthly_score_history_backup() below). A no-op if this copy
    has no score_history table (e.g. the monthly copy, which keeps the
    FULL table and must never call this)."""
    conn = sqlite3.connect(db_path, timeout=30)
    try:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='score_history'"
        ).fetchone()
        if exists:
            cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
            conn.execute("DELETE FROM score_history WHERE day < ?", (cutoff,))
            conn.commit()
            conn.execute("VACUUM")
    finally:
        conn.close()


def _gzip_file(src_path):
    """Gzips `src_path` next to itself (same dir, .gz suffix) and
    returns the gzipped path."""
    dest_path = src_path + ".gz"
    with open(src_path, "rb") as f_in, gzip.open(dest_path, "wb") as f_out:
        shutil.copyfileobj(f_in, f_out)
    return dest_path


def _check_attachment_size(path, log):
    """TASK 77 size guard: logs `path`'s size unconditionally
    ("[db_backup] attachment <file> <bytes>"), and additionally logs
    "[db_backup] OVER LIMIT <file> <bytes>" when it exceeds
    MAILGUN_ATTACHMENT_LIMIT_BYTES. Returns (include: bool, size:
    int) - `include` is False when the file must be dropped from the
    email rather than sent silently over-limit; the caller is
    responsible for actually leaving it out of `attachments` and
    noting the drop in the email body."""
    size = os.path.getsize(path)
    name = os.path.basename(path)
    log(f"[db_backup] attachment {name} {size}")
    if size > MAILGUN_ATTACHMENT_LIMIT_BYTES:
        log(f"[db_backup] OVER LIMIT {name} {size}")
        return False, size
    return True, size


# -----------------------------------------------------------------
# Mailgun send - same per-module _cfg() convention as digest_engine.py/
# weekly_brief_engine.py/announce_engine.py, but with a `files=`
# multipart attachment, which none of those needed.
# -----------------------------------------------------------------

def _mailgun_cfg():
    domain = os.environ.get("MAILGUN_DOMAIN", "").strip()
    return {
        "api_key": os.environ.get("MAILGUN_API_KEY", "").strip(),
        "domain": domain,
        "from": os.environ.get("MAILGUN_FROM", "").strip()
                or (f"StocksDeepDive <backups@{domain}>" if domain else ""),
        "base_url": (
            os.environ.get("MAILGUN_BASE_URL", "").strip()
            or os.environ.get("MAILGUN_API_BASE_URL", "").strip().removesuffix("/v3")
            or "https://api.mailgun.net"
        ).rstrip("/"),
    }


def is_configured():
    c = _mailgun_cfg()
    return bool(c["api_key"] and c["domain"] and ai_gate.owner_email())


def _send_backup_email(subject, text_body, attachment_paths):
    """Sends one email with 1+ gzipped file attachments to the owner's
    own inbox. Raises on failure - the caller (run_nightly_backup/
    run_backup_now) is what turns that into the "log + notify once"
    behaviour, not this function."""
    c = _mailgun_cfg()
    files = []
    opened = []
    try:
        for path in attachment_paths:
            fh = open(path, "rb")
            opened.append(fh)
            files.append(("attachment", (os.path.basename(path), fh, "application/gzip")))
        resp = requests.post(
            f"{c['base_url']}/v3/{c['domain']}/messages",
            auth=("api", c["api_key"]),
            data={"from": c["from"], "to": [ai_gate.owner_email()],
                  "subject": subject, "text": text_body},
            files=files,
            timeout=60,
        )
        resp.raise_for_status()
    finally:
        for fh in opened:
            fh.close()


def _weekly_extra_files():
    """compounder_data.json / company_potential_corrections.json from
    the VOLUME (not the repo - Part 9 removed the stale repo-tracked
    seed copies), gzipped into a temp dir, if present. Returns a list
    of gzipped paths (possibly empty - a fresh/wiped volume with no
    research data yet is not an error here)."""
    if build_compounder_data is None:
        return []
    try:
        data_dir = build_compounder_data._cp_data_dir()
    except Exception:
        return []
    out = []
    for fname in ("compounder_data.json", "company_potential_corrections.json"):
        src = os.path.join(data_dir, fname)
        if os.path.exists(src):
            try:
                tmp = os.path.join(tempfile.gettempdir(), fname)
                shutil.copyfile(src, tmp)
                out.append(_gzip_file(tmp))
            except Exception:
                pass
    return out


# -----------------------------------------------------------------
# The main nightly/Sunday job.
# -----------------------------------------------------------------

def _run_backup(log):
    """Does the actual nightly work and returns (ok: bool, message:
    str, detail: dict). Never raises - every failure path is caught
    and turned into (False, message, {}); the caller decides what to
    do about a failure (log-and-notify-once for the nightly job, a
    plain error message for the admin "Backup now" button).

    TASK 77: always builds the SAME deterministic nightly attachment
    (BACKUP_TABLES["USER"] + ["HISTORY"] + ["AI"], score_history
    trimmed to SCORE_HISTORY_RETENTION_DAYS days, top100_scores.prompt
    NULLed) - no more size-based full/critical fallback. On Sunday
    (UTC) a second attachment (BACKUP_TABLES["ANALYTICS"]) and the
    pre-existing weekly extras (compounder_data.json/corrections) are
    added to the SAME email. See _check_attachment_size()'s own
    docstring for the per-attachment size guard."""
    today = datetime.now(timezone.utc)
    is_analytics_day = today.weekday() == ANALYTICS_WEEKDAY
    date_str = today.strftime("%Y-%m-%d")

    tmp_dir = tempfile.mkdtemp(prefix="sdd_backup_")
    try:
        full_copy_path = os.path.join(tmp_dir, "stocksdeepdive_full.db")
        try:
            _vacuum_into(full_copy_path)
        except Exception as e:
            return False, f"VACUUM INTO failed: {type(e).__name__}: {e}", {}

        # Commit K: runs BEFORE any table-allowlist trim, on the
        # pristine full copy, so every later branch (nightly AND
        # Sunday analytics) inherits the exclusion automatically - see
        # EXCLUDED_FROM_EMAIL_TABLES/_exclude_sensitive_tables()'s own
        # comments for why.
        try:
            _exclude_sensitive_tables(full_copy_path)
        except Exception as e:
            return False, f"sensitive-table exclusion failed: {type(e).__name__}: {e}", {}

        nightly_tables = (
            BACKUP_TABLES["USER"] + BACKUP_TABLES["HISTORY"] + BACKUP_TABLES["AI"]
        )
        nightly_copy_path = os.path.join(tmp_dir, "stocksdeepdive_nightly.db")
        shutil.copyfile(full_copy_path, nightly_copy_path)
        try:
            _trim_to_tables(nightly_copy_path, nightly_tables)
            _null_column_values(nightly_copy_path, "top100_scores", "prompt")
            _trim_score_history_days(nightly_copy_path, SCORE_HISTORY_RETENTION_DAYS)
        except Exception as e:
            return False, f"nightly-tables trim failed: {type(e).__name__}: {e}", {}
        nightly_gz_path = _gzip_file(nightly_copy_path)
        nightly_gz_name = f"stocksdeepdive_backup_{date_str}.db.gz"
        renamed_nightly = os.path.join(tmp_dir, nightly_gz_name)
        shutil.copyfile(nightly_gz_path, renamed_nightly)

        attachments = []
        dropped = []  # [(name, size_bytes), ...]
        included, size = _check_attachment_size(renamed_nightly, log)
        if included:
            attachments.append(renamed_nightly)
        else:
            dropped.append((nightly_gz_name, size))

        if is_analytics_day:
            analytics_copy_path = os.path.join(tmp_dir, "stocksdeepdive_analytics.db")
            shutil.copyfile(full_copy_path, analytics_copy_path)
            try:
                _trim_to_tables(analytics_copy_path, BACKUP_TABLES["ANALYTICS"])
            except Exception as e:
                return False, f"analytics-tables trim failed: {type(e).__name__}: {e}", {}
            analytics_gz_path = _gzip_file(analytics_copy_path)
            analytics_gz_name = f"stocksdeepdive_analytics_{date_str}.db.gz"
            renamed_analytics = os.path.join(tmp_dir, analytics_gz_name)
            shutil.copyfile(analytics_gz_path, renamed_analytics)
            included, size = _check_attachment_size(renamed_analytics, log)
            if included:
                attachments.append(renamed_analytics)
            else:
                dropped.append((analytics_gz_name, size))

        extra_note = ""
        if is_analytics_day:
            extras = []
            for extra_path in _weekly_extra_files():
                included, size = _check_attachment_size(extra_path, log)
                if included:
                    extras.append(extra_path)
                else:
                    dropped.append((os.path.basename(extra_path), size))
            attachments.extend(extras)
            if extras:
                extra_note = " + research data/corrections cache"

        coverage = "USER + HISTORY + AI tables" + (
            " + ANALYTICS" if is_analytics_day else ""
        )
        body_lines = [
            f"Automated backup for {date_str}.",
            f"Tables included tonight: "
            + ", ".join(nightly_tables + (BACKUP_TABLES["ANALYTICS"] if is_analytics_day else [])) + ".",
            f"Not included (rebuilt by the nightly scan): {', '.join(REBUILDABLE_TABLES)}.",
            f"score_history is trimmed to the last {SCORE_HISTORY_RETENTION_DAYS} days here - "
            "the full history goes out separately on the 1st of each month.",
        ]
        if dropped:
            for name, size in dropped:
                body_lines.append(
                    f"NOTE: {name} ({size:,} bytes) was dropped from this email - it "
                    "exceeded Mailgun's size limit. See the admin panel/logs."
                )
        if not attachments and not dropped:
            return False, "No attachments were built at all (unexpected).", {}

        subject = f"StocksDeepDive backup — {date_str} ({coverage}{extra_note})"
        if attachments:
            _send_backup_email(subject, "\n".join(body_lines), attachments)
        else:
            # Every candidate attachment was over the size limit - still
            # send the email so the owner sees the drop notice, just with
            # nothing attached (never silently skip the whole night).
            _send_backup_email(subject + " [no attachments - see body]", "\n".join(body_lines), [])

        return True, f"Backup sent ({coverage}{extra_note}).", {
            "coverage": coverage, "attachments_sent": len(attachments),
            "attachments_dropped": [n for n, _ in dropped],
        }
    except Exception as e:
        return False, f"{type(e).__name__}: {e}", {}
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _run_monthly_score_history_backup(log):
    """1st-of-the-month (UTC) job - a SEPARATE, independent email
    carrying the FULL (untrimmed) score_history table, since the
    nightly/Sunday backup only ever carries the last
    SCORE_HISTORY_RETENTION_DAYS days of it. Never raises - same
    (ok, message, detail) convention as _run_backup() above. Best-
    effort: a failure here is logged but never raised into, or allowed
    to affect, the nightly job's own state tracking (see
    run_nightly_backup()'s own call site)."""
    today = datetime.now(timezone.utc)
    date_str = today.strftime("%Y-%m-%d")
    tmp_dir = tempfile.mkdtemp(prefix="sdd_backup_monthly_")
    try:
        full_copy_path = os.path.join(tmp_dir, "stocksdeepdive_full.db")
        try:
            _vacuum_into(full_copy_path)
        except Exception as e:
            return False, f"VACUUM INTO failed: {type(e).__name__}: {e}", {}
        try:
            _exclude_sensitive_tables(full_copy_path)
            _trim_to_tables(full_copy_path, ["score_history"])
        except Exception as e:
            return False, f"score_history trim failed: {type(e).__name__}: {e}", {}

        gz_path = _gzip_file(full_copy_path)
        gz_name = f"stocksdeepdive_score_history_{date_str}.db.gz"
        renamed = os.path.join(tmp_dir, gz_name)
        shutil.copyfile(gz_path, renamed)

        included, size = _check_attachment_size(renamed, log)
        body_lines = [
            f"Monthly full score_history backup for {date_str}.",
            "This is the FULL score_history table, untrimmed - the nightly/"
            f"Sunday backup only ever carries the last {SCORE_HISTORY_RETENTION_DAYS} days of it.",
        ]
        if not included:
            body_lines.append(
                f"NOTE: {gz_name} ({size:,} bytes) was dropped from this email - it "
                "exceeded Mailgun's size limit. See the admin panel/logs."
            )
        subject = f"StocksDeepDive monthly score_history backup — {date_str}"
        _send_backup_email(subject, "\n".join(body_lines), [renamed] if included else [])
        return True, "Monthly score_history backup sent.", {"size_bytes": size, "included": included}
    except Exception as e:
        return False, f"{type(e).__name__}: {e}", {}
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def run_nightly_backup(log=print):
    """Scheduler entry point - see scheduler_engine.py's own docstring
    for where this is wired in (after the nightly scan window). Never
    raises. Records last-success/last-failure state for the admin
    panel and the once-not-nightly failure-email rule. On the 1st of
    the month (UTC), also runs the separate full-score_history email
    (_run_monthly_score_history_backup()) - tracked under its own
    state keys, best-effort, never allowed to override or mask the
    main nightly job's own success/failure state."""
    if not is_configured():
        log("[db_backup] skipped - Mailgun not configured or owner email unavailable")
        return
    state = _load_state()
    today = datetime.now(timezone.utc)
    today_str = today.strftime("%Y-%m-%d")
    ok, message, detail = _run_backup(log)
    if ok:
        state["last_success_date"] = today_str
        state["last_success_detail"] = detail
        state.pop("last_failure_streak_notified", None)
        _save_state(state)
        log(f"[db_backup] {message}")
    else:
        log(f"[db_backup] FAILED: {message}")
        state["last_failure_date"] = today_str
        state["last_failure_message"] = message
        if not state.get("last_failure_streak_notified"):
            # "Failures logged and emailed once (not nightly spam)" -
            # notify on the FIRST failure of a new streak only; a
            # streak ends the moment a run next succeeds (cleared
            # above). Best-effort: if even the failure notification
            # can't send (e.g. Mailgun itself is down), it's still
            # logged - the loud admin-panel warning is the fallback.
            try:
                c = _mailgun_cfg()
                if c["api_key"] and c["domain"] and ai_gate.owner_email():
                    requests.post(
                        f"{c['base_url']}/v3/{c['domain']}/messages",
                        auth=("api", c["api_key"]),
                        data={"from": c["from"], "to": [ai_gate.owner_email()],
                              "subject": f"⚠ StocksDeepDive backup FAILED — {today_str}",
                              "text": f"Tonight's database backup failed: {message}\n\n"
                                      "This message is sent once per failure streak - it "
                                      "won't repeat every night until a backup next "
                                      "succeeds. Check the admin panel's backup status line."},
                        timeout=20,
                    )
                state["last_failure_streak_notified"] = True
            except Exception:
                pass
        _save_state(state)

    if today.day == 1:
        try:
            m_ok, m_message, m_detail = _run_monthly_score_history_backup(log)
            state = _load_state()  # re-read - the nightly save above may have changed it
            if m_ok:
                state["last_monthly_score_history_success_date"] = today_str
                log(f"[db_backup] {m_message}")
            else:
                state["last_monthly_score_history_failure_date"] = today_str
                state["last_monthly_score_history_failure_message"] = m_message
                log(f"[db_backup] monthly score_history FAILED: {m_message}")
            _save_state(state)
        except Exception as e:
            # Belt-and-suspenders - _run_monthly_score_history_backup()
            # itself never raises, but a broken state read/write here
            # must never take down the nightly job's own already-saved
            # state.
            log(f"[db_backup] monthly score_history backup raised unexpectedly: "
                f"{type(e).__name__}: {e}")


def run_backup_now(log=print):
    """Admin panel's "Backup now" button - runs the EXACT SAME
    construction the scheduler would run tonight (TASK 77: there's no
    more separate "forced full" mode to ask for, since every night's
    set is already the real, complete USER+HISTORY+AI set, plus
    ANALYTICS/weekly-extras if today happens to be Sunday). Returns
    (ok, message) for a direct st.success/st.error in the panel.
    Still updates the same state the nightly job does, so the status
    line reflects it."""
    if not is_configured():
        return False, "Mailgun isn't configured yet (MAILGUN_API_KEY/MAILGUN_DOMAIN), so backups can't be sent."
    state = _load_state()
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    ok, message, detail = _run_backup(log)
    if ok:
        state["last_success_date"] = today_str
        state["last_success_detail"] = detail
        state.pop("last_failure_streak_notified", None)
    else:
        state["last_failure_date"] = today_str
        state["last_failure_message"] = message
    _save_state(state)
    return ok, message


def last_backup_status():
    """{"last_success_date", "days_since_success" (None if never),
    "is_stale" (>= STALE_WARNING_DAYS since the last success, or never
    succeeded at all), "last_failure_date", "last_failure_message"} -
    everything app.py's admin panel needs for its status line + loud
    warning, with no SQL/file access of its own."""
    state = _load_state()
    last_success = state.get("last_success_date")
    days_since = None
    if last_success:
        try:
            d = datetime.strptime(last_success, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            days_since = (datetime.now(timezone.utc) - d).days
        except ValueError:
            days_since = None
    is_stale = (days_since is None) or (days_since >= STALE_WARNING_DAYS)
    return {
        "last_success_date": last_success,
        "days_since_success": days_since,
        "is_stale": is_stale,
        "last_failure_date": state.get("last_failure_date"),
        "last_failure_message": state.get("last_failure_message"),
    }


# -----------------------------------------------------------------
# Owner-only manual "Download full backup" button (TASK 71 Part A,
# Director-directed, owner-approved, 21 Nov 2026). UNCHANGED by
# TASK 77 (23 Nov 2026) - see that task's own instruction item 4.
# -----------------------------------------------------------------

def build_owner_download(log=print):
    """Builds the SAME sanitised copy the nightly email path uses
    (VACUUM INTO -> _exclude_sensitive_tables() -> gzip) for the Admin
    dashboard's "Download full backup" button - but with NO table
    allowlist, NO size limit and NO critical-tables fallback (unlike
    _run_backup() above): the owner always gets the complete sanitised
    database, however large.

    Returns (ok, message, result). On success, result is {"data": bytes,
    "filename": str, "size_bytes": int, "built_at": ISO-8601 UTC str} -
    `data` is the gzipped file's bytes, read into memory BEFORE this
    function's own temp directory is deleted (same tempfile.mkdtemp() +
    `finally: shutil.rmtree(...)` pattern as _run_backup() above), so the
    caller (app.py's Admin dashboard, via st.download_button) never has
    to manage any file cleanup of its own and no copy is ever left on
    disk, or written to any web-served path, once this returns. On
    failure, result is None and message explains why - including a
    refusal (ok=False) while the nightly backup job's own lock is held
    (scheduler_engine.backup_job_lock_active() - a read-only, non-
    blocking check, never acquired/released here), so a manual download
    never runs its own VACUUM INTO against the live DB at the same
    moment the nightly job's is already mid-flight. Never raises -
    every failure path is caught and returned as (False, message, None),
    same convention as _run_backup() above."""
    try:
        import scheduler_engine
        if scheduler_engine.backup_job_lock_active():
            return False, (
                "The nightly backup job is running right now - try again "
                "in a few minutes."
            ), None
    except Exception:
        # Fail open: a broken lock check should never block a manual
        # download the owner is actively waiting on - same reasoning as
        # every other best-effort check in this module (e.g. _weekly_
        # extra_files() above).
        pass

    built_at = datetime.now(timezone.utc)
    date_str = built_at.strftime("%Y-%m-%d")
    tmp_dir = tempfile.mkdtemp(prefix="sdd_owner_backup_")
    try:
        full_copy_path = os.path.join(tmp_dir, "stocksdeepdive_full.db")
        try:
            _vacuum_into(full_copy_path)
        except Exception as e:
            return False, f"VACUUM INTO failed: {type(e).__name__}: {e}", None
        try:
            _exclude_sensitive_tables(full_copy_path)
        except Exception as e:
            return False, f"sensitive-table exclusion failed: {type(e).__name__}: {e}", None
        gz_path = _gzip_file(full_copy_path)
        size_bytes = os.path.getsize(gz_path)
        with open(gz_path, "rb") as f:
            data = f.read()
        # Log the SIZE only, never contents - same privacy posture as
        # every other log line in this module.
        log(f"[db_backup] owner download built: {size_bytes:,} bytes")
        return True, "Backup built.", {
            "data": data,
            "filename": f"stocksdeepdive_full_{date_str}.db.gz",
            "size_bytes": size_bytes,
            "built_at": built_at.isoformat(),
        }
    except Exception as e:
        return False, f"{type(e).__name__}: {e}", None
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
