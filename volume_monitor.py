"""
volume_monitor.py

Mega-batch Part 16: a full Railway Volume fails SILENTLY - every SQLite
write starts raising ("database or disk is full") while every page that
doesn't write (most of the site) keeps rendering perfectly normally, so
sign-ups, portfolio saves and even the Part-10 nightly backup itself can
all be quietly broken for days before anyone notices. This module adds:

  1. A nightly usage check (shutil.disk_usage on the volume mount path,
     same _data_dir() convention as every other *_store.py/*_engine.py
     module on this site) - a monthly email alert (same Mailgun path as
     db_backup_engine.py) once usage crosses WARNING_THRESHOLD_PCT, plus
     a loud admin-panel warning; at CRITICAL_THRESHOLD_PCT the warning
     names which features will actually start failing (anything that
     writes to stocksdeepdive.db: sign-ups, portfolio/watchlist/alert/
     checklist saves, the nightly backup, this very monitor's own state
     file).
  2. Bounded, conservative retention pruning - the ONLY two categories
     the instruction names:
       a) the research-data rebuild archive (build_compounder_data's
          compounder_archive/ folder) capped at the most recent
          ARCHIVE_KEEP_COUNT rebuilds. This is also list_archived_
          snapshots()'s own source (the Research page's rebuild-history
          picker) - pruning the OLDEST files first means the picker just
          shows a shorter list, never a broken one.
       b) four TTL-based caches, each pruned once a row/file is older
          than STALE_MULTIPLIER times ITS OWN normal TTL (a cache is
          disposable by design once genuinely long-expired - nothing
          still treats a row/file that old as valid data, since every
          one of these caches already has its OWN, separate freshness
          check on the read side that would refuse it anyway):
            - auto_compounder_engine's per-ticker section cache (file)
            - fundamentals_data's per-ticker bundle cache (file)
            - stress_engine's long-history + result caches (SQLite)
            - etf_insights' fund-facts cache (SQLite)

PRUNE_NEVER (checked explicitly - see each function's own docstring for
why it was excluded): the live account/portfolio/watchlist/alert/
checklist/usage tables (db_backup_engine.CRITICAL_TABLES), score_history
(the future track record), snapshot_store's snapshots, and anything
else user- or owner-authored. Nothing in this module ever runs a DELETE
against any table outside the four explicitly-named cache tables above,
and nothing in this module ever touches stocksdeepdive.db's file itself
(only specific rows within specific cache tables).
"""

import json
import os
import shutil
import sqlite3
from datetime import datetime, timezone, timedelta

import requests

import ai_gate


def _data_dir():
    return os.environ.get("RAILWAY_VOLUME_MOUNT_PATH") or os.path.dirname(__file__)


STATE_PATH = os.path.join(_data_dir(), "volume_monitor_state.json")

WARNING_THRESHOLD_PCT = 80.0
CRITICAL_THRESHOLD_PCT = 95.0

# A cache row/file is pruned once it's this many times its OWN normal
# TTL old - generous on purpose (this is retention hygiene, not an
# aggressive sweep; every one of these caches already refuses a stale
# entry on the read side well before this multiplier kicks in).
STALE_MULTIPLIER = 4

# The rebuild-history picker's own source folder keeps only its most
# recent N snapshots.
ARCHIVE_KEEP_COUNT = 24

# Features that stop working once stocksdeepdive.db can no longer accept
# writes - shown verbatim in the >=95% admin warning so it "states
# plainly which features will start failing", per the instruction.
_WRITE_DEPENDENT_FEATURES = (
    "new sign-ups and sign-ins",
    "saving/editing portfolios, watchlists, alerts and checklists",
    "the nightly off-site database backup (Part 10)",
    "this volume monitor's own state (so its alerts may stop firing too)",
)


# -----------------------------------------------------------------
# Small state file - same _load_state/_save_state convention as
# db_backup_engine.py/scheduler_engine.py (atomic write via .tmp + replace).
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
# Usage.
# -----------------------------------------------------------------

def disk_usage():
    """{"used_bytes", "total_bytes", "pct_used"} for wherever _data_dir()
    resolves (the Railway Volume mount path in production; this
    module's own directory when RAILWAY_VOLUME_MOUNT_PATH isn't set,
    e.g. local dev) - None if shutil.disk_usage itself fails (a path
    that doesn't exist yet, unlikely but not worth crashing over)."""
    try:
        usage = shutil.disk_usage(_data_dir())
    except OSError:
        return None
    total, used = usage.total, usage.used
    return {
        "used_bytes": used,
        "total_bytes": total,
        "pct_used": (used / total * 100.0) if total else 0.0,
    }


def usage_breakdown(top_n=15):
    """[{"name", "bytes"}], largest first - top-level directories/files
    directly under _data_dir(), for the report's "actual size and
    current usage breakdown" ask. A directory's size is the recursive
    sum of every file under it; unreadable entries are skipped rather
    than failing the whole breakdown."""
    base = _data_dir()
    out = []
    try:
        entries = os.listdir(base)
    except OSError:
        return out
    for name in entries:
        full = os.path.join(base, name)
        size = 0
        try:
            if os.path.isdir(full):
                for root, _dirs, files in os.walk(full):
                    for fn in files:
                        try:
                            size += os.path.getsize(os.path.join(root, fn))
                        except OSError:
                            pass
            else:
                size = os.path.getsize(full)
        except OSError:
            continue
        out.append({"name": name, "bytes": size})
    out.sort(key=lambda r: r["bytes"], reverse=True)
    return out[:top_n]


# -----------------------------------------------------------------
# Mailgun alert - same per-module _cfg() convention as db_backup_engine.py.
# -----------------------------------------------------------------

def _mailgun_cfg():
    domain = os.environ.get("MAILGUN_DOMAIN", "").strip()
    return {
        "api_key": os.environ.get("MAILGUN_API_KEY", "").strip(),
        "domain": domain,
        "from": os.environ.get("MAILGUN_FROM", "").strip()
                or (f"StocksDeepDive <alerts@{domain}>" if domain else ""),
        "base_url": (
            os.environ.get("MAILGUN_BASE_URL", "").strip()
            or os.environ.get("MAILGUN_API_BASE_URL", "").strip().removesuffix("/v3")
            or "https://api.mailgun.net"
        ).rstrip("/"),
    }


def _send_alert_email(subject, text_body):
    c = _mailgun_cfg()
    owner = ai_gate.owner_email()
    if not (c["api_key"] and c["domain"] and owner):
        return False
    resp = requests.post(
        f"{c['base_url']}/v3/{c['domain']}/messages",
        auth=("api", c["api_key"]),
        data={"from": c["from"], "to": [owner], "subject": subject, "text": text_body},
        timeout=20,
    )
    resp.raise_for_status()
    return True


def _maybe_send_monthly_alert(usage, log=print):
    """"At >=80% used, ONE email alert to the owner per month" - tracked
    by year-month string in state, so this fires at most once even if
    the nightly check runs every night while usage stays above the
    threshold, and fires again (once) the following calendar month if
    it's still above threshold then."""
    if usage["pct_used"] < WARNING_THRESHOLD_PCT:
        return
    state = _load_state()
    month_key = datetime.now(timezone.utc).strftime("%Y-%m")
    if state.get("last_alert_month") == month_key:
        return
    pct = usage["pct_used"]
    used_mb = usage["used_bytes"] / (1024 * 1024)
    total_mb = usage["total_bytes"] / (1024 * 1024)
    lines = [
        f"The StocksDeepDive Volume is at {pct:.0f}% used "
        f"({used_mb:,.0f} MB of {total_mb:,.0f} MB).",
    ]
    if pct >= CRITICAL_THRESHOLD_PCT:
        lines.append("")
        lines.append(
            f"This is above the {CRITICAL_THRESHOLD_PCT:.0f}% critical "
            "threshold - if the volume actually fills up, these features "
            "start failing:"
        )
        lines.extend(f"  - {f}" for f in _WRITE_DEPENDENT_FEATURES)
    lines.append("")
    lines.append("Check the admin panel's Volume gauge for the current figure.")
    try:
        sent = _send_alert_email(
            f"⚠ StocksDeepDive Volume at {pct:.0f}% used", "\n".join(lines),
        )
        if sent:
            state["last_alert_month"] = month_key
            _save_state(state)
            log(f"[volume_monitor] usage alert sent ({pct:.0f}%)")
        else:
            log("[volume_monitor] usage alert NOT sent - Mailgun not configured")
    except Exception as e:
        log(f"[volume_monitor] usage alert failed to send: {e}")


# -----------------------------------------------------------------
# Retention pruning.
# -----------------------------------------------------------------

def _prune_research_archive(log=print):
    """Keeps only the ARCHIVE_KEEP_COUNT most recent rebuild snapshots
    in build_compounder_data's own archive folder. Filenames embed a
    sortable ISO-ish timestamp (see archive_current_snapshot()'s own
    "safe" transform - ':' -> '-', still lexicographically chronological)
    - the exact same ordering list_archived_snapshots() already relies
    on for its own newest-first sort - so a plain sorted() here needs no
    separate date parsing. Never touches anything else in the data
    directory."""
    try:
        import build_compounder_data
    except Exception as e:
        log(f"[volume_monitor] could not import build_compounder_data: {e}")
        return 0
    try:
        archive_dir = build_compounder_data._cp_archive_dir()
        files = sorted(
            fn for fn in os.listdir(archive_dir)
            if fn.startswith("compounder_data_") and fn.endswith(".json")
        )
    except OSError:
        return 0
    if len(files) <= ARCHIVE_KEEP_COUNT:
        return 0
    oldest_first_excess = files[:-ARCHIVE_KEEP_COUNT]  # keeps the newest ARCHIVE_KEEP_COUNT
    pruned = 0
    for fn in oldest_first_excess:
        try:
            os.remove(os.path.join(archive_dir, fn))
            pruned += 1
        except OSError:
            pass
    if pruned:
        log(f"[volume_monitor] pruned {pruned} old research-archive snapshot(s)")
    return pruned


def _prune_json_cache_dir(cache_dir, timestamp_key_path, ttl_seconds, log=print, label=""):
    """Generic pruner for a directory of per-ticker JSON cache files
    whose read side already knows how to find its own "how fresh is
    this" timestamp at a fixed nested key. `timestamp_key_path` is the
    tuple of dict keys to walk (e.g. ("_meta", "generated_at")).

    A file whose timestamp can't be read at all (corrupt JSON, missing
    key - the read side would ALSO have rejected it as a cache miss)
    falls back to its own file mtime instead of being skipped forever -
    conservative either way: it's only removed once it's actually old
    by one clock or the other, never guessed at."""
    if not os.path.isdir(cache_dir):
        return 0
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=ttl_seconds * STALE_MULTIPLIER)
    pruned = 0
    for fn in os.listdir(cache_dir):
        if not fn.endswith(".json"):
            continue
        path = os.path.join(cache_dir, fn)
        age_dt = None
        try:
            with open(path) as f:
                obj = json.load(f)
            node = obj
            for k in timestamp_key_path:
                node = (node or {}).get(k)
            if node:
                age_dt = datetime.fromisoformat(node)
                if age_dt.tzinfo is None:
                    age_dt = age_dt.replace(tzinfo=timezone.utc)
        except Exception:
            age_dt = None
        if age_dt is None:
            try:
                age_dt = datetime.fromtimestamp(os.path.getmtime(path), tz=timezone.utc)
            except OSError:
                continue
        if age_dt < cutoff:
            try:
                os.remove(path)
                pruned += 1
            except OSError:
                pass
    if pruned:
        log(f"[volume_monitor] pruned {pruned} stale {label or cache_dir} file(s)")
    return pruned


def _prune_auto_compounder_cache(log=print):
    """auto_compounder_engine's per-ticker Compounder View section
    cache - TTL 24h, so pruned once a file is >96h old."""
    try:
        import auto_compounder_engine as ace
    except Exception as e:
        log(f"[volume_monitor] could not import auto_compounder_engine: {e}")
        return 0
    return _prune_json_cache_dir(
        ace._cache_dir(), ("_meta", "generated_at"), ace._CACHE_TTL_SECONDS,
        log=log, label="auto_cv_sections",
    )


def _prune_fundamentals_cache(log=print):
    """fundamentals_data's per-ticker Yahoo bundle cache - TTL 24h, so
    pruned once a file is >96h old."""
    try:
        import fundamentals_data as fd
    except Exception as e:
        log(f"[volume_monitor] could not import fundamentals_data: {e}")
        return 0
    return _prune_json_cache_dir(
        fd._cache_dir(), ("meta", "fetched_at"), fd._CACHE_TTL_SECONDS,
        log=log, label="auto_cv_cache",
    )


def _prune_sqlite_table(db_path, table, ttl_hours, log=print):
    """DELETEs rows from ONE named cache table whose created_at is older
    than ttl_hours * STALE_MULTIPLIER. Every table this is called with
    (see the two callers below) stores nothing but a re-fetchable cache
    row keyed by ticker/cache-key - never account, portfolio, or any
    other user-authored data, and this function is never called with
    any other table name."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=ttl_hours * STALE_MULTIPLIER)).isoformat()
    try:
        conn = sqlite3.connect(db_path, timeout=10)
        try:
            cur = conn.execute(f"DELETE FROM {table} WHERE created_at < ?", (cutoff,))
            conn.commit()
            return max(cur.rowcount, 0)
        finally:
            conn.close()
    except Exception as e:
        log(f"[volume_monitor] could not prune {table}: {e}")
        return 0


def _prune_stress_engine_caches(log=print):
    try:
        import stress_engine as se
    except Exception as e:
        log(f"[volume_monitor] could not import stress_engine: {e}")
        return 0
    n1 = _prune_sqlite_table(se.DB_PATH, se._LONG_HISTORY_TABLE, se.LONG_HISTORY_TTL_HOURS, log=log)
    n2 = _prune_sqlite_table(se.DB_PATH, se._RESULT_CACHE_TABLE, se.RESULT_TTL_HOURS, log=log)
    total = n1 + n2
    if total:
        log(f"[volume_monitor] pruned {total} stale stress_engine cache row(s)")
    return total


def _prune_etf_insights_cache(log=print):
    try:
        import etf_insights as ei
    except Exception as e:
        log(f"[volume_monitor] could not import etf_insights: {e}")
        return 0
    n = _prune_sqlite_table(ei.DB_PATH, "etf_fund_facts", ei.FUND_FACTS_TTL_HOURS, log=log)
    if n:
        log(f"[volume_monitor] pruned {n} stale etf_insights fund-facts row(s)")
    return n


def run_retention_pruning(log=print):
    """Runs every pruning category and returns {"category": count_pruned}
    for the report/admin panel. Each category is independently wrapped
    (in its own try/except further down its call chain) so one failing
    category never blocks the others."""
    return {
        "research_archive": _prune_research_archive(log=log),
        "auto_compounder_cache": _prune_auto_compounder_cache(log=log),
        "fundamentals_cache": _prune_fundamentals_cache(log=log),
        "stress_engine_cache": _prune_stress_engine_caches(log=log),
        "etf_insights_cache": _prune_etf_insights_cache(log=log),
    }


# -----------------------------------------------------------------
# Scheduler entry point + admin panel read.
# -----------------------------------------------------------------

def run_retention_pruning_and_record(log=print):
    """run_retention_pruning() plus recording the result to state, so
    the admin panel's "last retention prune" line reflects EITHER a
    nightly run or an admin's manual "Run retention prune now" click -
    both call this, not run_retention_pruning() directly."""
    pruned = run_retention_pruning(log=log)
    state = _load_state()
    state["last_prune_date"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    state["last_prune_counts"] = pruned
    _save_state(state)
    return pruned


def run_nightly_check(log=print):
    """Scheduler entry point (see scheduler_engine.py's own wiring) -
    checks usage (sending the monthly alert if warranted), then runs
    retention pruning. Never raises - every sub-step is already
    individually guarded; this is one more outer safety net so a
    surprise failure here can't take the scheduler thread down."""
    try:
        usage = disk_usage()
        if usage is not None:
            _maybe_send_monthly_alert(usage, log=log)
        else:
            log("[volume_monitor] disk_usage() unavailable - skipping usage alert")
    except Exception as e:
        log(f"[volume_monitor] usage check failed: {e}")
    try:
        run_retention_pruning_and_record(log=log)
    except Exception as e:
        log(f"[volume_monitor] retention pruning failed: {e}")


def admin_summary():
    """Everything app.py's admin panel needs for the one-line gauge +
    warnings + last-prune line, with no filesystem/SQL work beyond
    disk_usage() itself."""
    usage = disk_usage()
    state = _load_state()
    return {
        "usage": usage,
        "is_warning": bool(usage and usage["pct_used"] >= WARNING_THRESHOLD_PCT),
        "is_critical": bool(usage and usage["pct_used"] >= CRITICAL_THRESHOLD_PCT),
        "last_prune_date": state.get("last_prune_date"),
        "last_prune_counts": state.get("last_prune_counts"),
        "write_dependent_features": list(_WRITE_DEPENDENT_FEATURES),
    }
