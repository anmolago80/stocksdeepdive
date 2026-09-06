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

DAILY vs WEEKLY, SIZE FALLBACK: every night (scheduled from
scheduler_engine, after the scan window), a full VACUUM INTO copy is
attempted and gzipped. If the gzipped size fits
MAILGUN_ATTACHMENT_LIMIT_BYTES, it's emailed as
stocksdeepdive_backup_YYYY-MM-DD.db.gz - every table, every night. If
a full copy is ever too large for that limit, the job falls back
automatically: a FULL backup only once a week (WEEKLY_FULL_WEEKDAY),
and on every OTHER night a CRITICAL-TABLES-ONLY dump (CRITICAL_TABLES
below - built by taking the same VACUUM INTO copy and dropping every
table not on that list, then re-VACUUMing to actually shrink the
file). Both paths are still real, ordinary .db.gz SQLite files - never
a JSON/CSV re-encoding - so the restore steps in the mega-batch's
final report are identical either way: gunzip, drop the file in as
stocksdeepdive.db on a fresh volume, restart the service.

CRITICAL_TABLES is this module's own reading of the spec's "every
account, portfolio, watchlist, alert, checklist and usage record" -
every table holding irreplaceable user- or owner-authored data or
account state. Everything left off that list is a cache, a fetch log,
or scan-derived history that rebuilds itself from live re-scans/re-
fetches over time (score_history, snapshots, insider/news caches,
etc.) - real to lose, but explicitly out of the spec's own critical
list, and this fallback only ever triggers when a full nightly copy
has become too large to email at all. Worth the owner's own review;
see this module's docstring reference in the final report.

WEEKLY EXTRAS: on WEEKLY_FULL_WEEKDAY's send (whichever of the two
paths above actually runs that night), compounder_data.json and
company_potential_corrections.json are additionally attached straight
from the volume (build_compounder_data._cp_data_dir()) if present -
Part 9 removed their stale repo-tracked seed copies, so the LIVE,
admin-rebuilt versions on the volume are what's actually worth
protecting, and both are small enough to ride along for free.

FAILURE HANDLING: any exception anywhere in the nightly job is caught,
logged, and emailed to the owner via the SAME Mailgun path AT MOST
ONCE per distinct failure streak (see _state()'s
"last_failure_streak_notified" flag - cleared the moment a run next
succeeds) - never a nightly spam stream for one persistent problem.

ADMIN PANEL: last_backup_status() and run_backup_now() are the two
public entry points app.py's admin panel calls for the "Backup now"
button and the "last backup sent: <date> [check/warn]" status line -
a loud warning once the last SUCCESSFUL backup is more than
STALE_WARNING_DAYS old.

PRIVACY: the backup is emailed ONLY to ai_gate.owner_email() - this
site's own owner inbox - via Mailgun, exactly like every other
outbound email this site already sends. Never written to, or
transmitted via, any third-party storage service; nothing here adds a
new data-sharing surface, so no /privacy update is needed.
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
# margin under that for MIME/multipart overhead and the second small
# weekly-extras attachment.
MAILGUN_ATTACHMENT_LIMIT_BYTES = 20 * 1024 * 1024
STALE_WARNING_DAYS = 3
WEEKLY_FULL_WEEKDAY = 6  # UTC Sunday - its own slot, doesn't collide
                          # with the digest (also Sunday, later hour)
                          # or the earnings refresh (Wednesday).

CRITICAL_TABLES = [
    "signups", "auth_codes", "auth_sessions",
    "portfolios", "portfolio_holdings", "portfolio_settings",
    "iv_overrides", "portfolio_seed_log",
    "watchlist", "alerts", "alert_hits_pending", "alert_eval_log",
    "checklists", "ai_usage", "ai_settings", "push_subscriptions",
    "card_blurbs", "author_positions",
    "blog_posts", "blog_comments", "blog_redirects", "followers",
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
# The consistent copy + optional critical-tables-only trim.
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


def _trim_to_critical_tables(db_path):
    """Opens the (already-copied, so this never touches the live DB)
    `db_path` and drops every table not in CRITICAL_TABLES, then
    VACUUMs it to actually shrink the file - used only on the size-
    fallback path (see module docstring)."""
    conn = sqlite3.connect(db_path, timeout=30)
    try:
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()]
        for t in tables:
            if t not in CRITICAL_TABLES:
                conn.execute(f'DROP TABLE IF EXISTS "{t}"')
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
# The main job.
# -----------------------------------------------------------------

def _run_backup(log, force_full=False):
    """Does the actual work and returns (ok: bool, message: str,
    detail: dict). Never raises - every failure path is caught and
    turned into (False, message, {}); the caller decides what to do
    about a failure (log-and-notify-once for the nightly job, a plain
    error message for the admin "Backup now" button)."""
    today = datetime.now(timezone.utc)
    is_weekly_slot = force_full or today.weekday() == WEEKLY_FULL_WEEKDAY
    date_str = today.strftime("%Y-%m-%d")

    tmp_dir = tempfile.mkdtemp(prefix="sdd_backup_")
    try:
        full_copy_path = os.path.join(tmp_dir, "stocksdeepdive_full.db")
        try:
            _vacuum_into(full_copy_path)
        except Exception as e:
            return False, f"VACUUM INTO failed: {type(e).__name__}: {e}", {}

        full_gz_path = _gzip_file(full_copy_path)
        full_gz_size = os.path.getsize(full_gz_path)
        used_fallback = False

        if full_gz_size <= MAILGUN_ATTACHMENT_LIMIT_BYTES:
            main_gz_path = full_gz_path
            main_gz_name = f"stocksdeepdive_backup_{date_str}.db.gz"
            coverage = "full database"
        elif is_weekly_slot:
            # Still send the full copy on the weekly slot even though
            # it's over the soft limit - Mailgun's own hard cap (not
            # this module's conservative margin) is the real ceiling;
            # let the send itself fail if it's genuinely too big, and
            # that failure is what the log+notify-once path is for.
            main_gz_path = full_gz_path
            main_gz_name = f"stocksdeepdive_backup_{date_str}.db.gz"
            coverage = "full database (over the soft size margin)"
        else:
            used_fallback = True
            critical_copy_path = os.path.join(tmp_dir, "stocksdeepdive_critical.db")
            shutil.copyfile(full_copy_path, critical_copy_path)
            try:
                _trim_to_critical_tables(critical_copy_path)
            except Exception as e:
                return False, f"critical-tables trim failed: {type(e).__name__}: {e}", {}
            main_gz_path = _gzip_file(critical_copy_path)
            main_gz_name = f"stocksdeepdive_backup_critical_{date_str}.db.gz"
            coverage = "critical tables only (full backup exceeded the size limit)"

        attachments = [main_gz_path]
        extra_note = ""
        if is_weekly_slot:
            extras = _weekly_extra_files()
            attachments.extend(extras)
            if extras:
                extra_note = " + research data/corrections cache"

        subject = f"StocksDeepDive backup — {date_str} ({coverage}{extra_note})"
        body_lines = [
            f"Automated backup for {date_str}.",
            f"Coverage: {coverage}.",
            f"Main attachment size (gzipped): {os.path.getsize(main_gz_path):,} bytes.",
        ]
        if used_fallback:
            body_lines.append(
                "NOTE: the full database is now too large to email whole - "
                "only the critical tables (accounts/portfolios/watchlists/"
                "alerts/checklists/usage) are included tonight. A full copy "
                "still goes out once a week regardless of size."
            )
        # Renamed on disk so each attachment keeps the requested naming
        # convention rather than a temp-dir basename.
        renamed_main = os.path.join(tmp_dir, main_gz_name)
        shutil.copyfile(main_gz_path, renamed_main)
        attachments[0] = renamed_main

        _send_backup_email(subject, "\n".join(body_lines), attachments)
        return True, f"Backup sent ({coverage}).", {
            "coverage": coverage, "used_fallback": used_fallback,
            "size_bytes": os.path.getsize(main_gz_path),
        }
    except Exception as e:
        return False, f"{type(e).__name__}: {e}", {}
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def run_nightly_backup(log=print):
    """Scheduler entry point - see scheduler_engine.py's own docstring
    for where this is wired in (after the nightly scan window). Never
    raises. Records last-success/last-failure state for the admin
    panel and the once-not-nightly failure-email rule."""
    if not is_configured():
        log("[db_backup] skipped - Mailgun not configured or owner email unavailable")
        return
    state = _load_state()
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
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


def run_backup_now(log=print):
    """Admin panel's "Backup now" button - forces a FULL backup
    regardless of weekday/size-fallback logic (the owner asked for it
    explicitly; give them the real thing). Returns (ok, message) for
    a direct st.success/st.error in the panel. Still updates the same
    state the nightly job does, so the status line reflects it."""
    if not is_configured():
        return False, "Mailgun isn't configured yet (MAILGUN_API_KEY/MAILGUN_DOMAIN), so backups can't be sent."
    state = _load_state()
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    ok, message, detail = _run_backup(log, force_full=True)
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
