"""
alert_store.py

Services batch, Part 1: metric alerts. Pure storage/query module (no
network, no Streamlit) for two small tables in the shared stocksdeepdive.db
- same _data_dir()/DB_PATH/_conn() convention as every other store module
(see follow_store.py's docstring for why SQLite over a hand-rolled file).

  alerts              - one row per "notify me when TICKER's METRIC does
                         X" rule a signed-in user created.
  alert_hits_pending  - a hit queued by alert_engine.py during tonight's
                         evaluation, waiting to be folded into that user's
                         ONE batched email + ONE push for the night (see
                         alert_engine.send_batched_notifications). Cleared
                         once sent.
  alert_eval_log      - which tickers alert_engine has already evaluated
                         TODAY, so the nightly "alert-only" extra pass
                         (Part 1's spec) only covers tickers with active
                         alerts that weren't already covered by a regular
                         universe scan tonight - see
                         alert_engine.run_extra_ticker_pass().

Every function does exactly one focused SQL statement; email is always
.strip().lower(), ticker always .strip().upper() - the same conventions
watchlist_store.py/follow_store.py use.
"""

import os
import sqlite3
from datetime import datetime, timezone, timedelta

MAX_ACTIVE_ALERTS_PER_USER = 20

NUMERIC_METRICS = {
    "mos_pct": "MOS %",
    "value_score": "Long Score",
    "quality": "Quality",
    "moat": "Moat",
    "price": "Price",
    "intrinsic_value": "Intrinsic Value",
}
CATEGORICAL_METRICS = {
    "moat_state": "Moat Erosion",
    "valuation_label": "Valuation",
}
ALL_METRICS = {**NUMERIC_METRICS, **CATEGORICAL_METRICS}

OPERATORS_NUMERIC = (">=", "<=", "crosses_above", "crosses_below")
OPERATORS_CATEGORICAL = ("becomes",)

MOAT_STATE_VALUES = ("eroding", "watch")
VALUATION_LABEL_VALUES = ("UNDERVALUED", "FAIR", "EXPENSIVE")

DEFAULT_COOLDOWN_HOURS = 72


def _data_dir():
    return os.environ.get("RAILWAY_VOLUME_MOUNT_PATH") or os.path.dirname(__file__)


DB_PATH = os.path.join(_data_dir(), "stocksdeepdive.db")


def _conn():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT NOT NULL,
            ticker TEXT NOT NULL,
            metric TEXT NOT NULL,
            operator TEXT NOT NULL,
            threshold TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1,
            cooldown_hours INTEGER NOT NULL DEFAULT 72,
            created_at TEXT NOT NULL,
            last_fired_at TEXT
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS alert_hits_pending (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            alert_id INTEGER NOT NULL,
            email TEXT NOT NULL,
            ticker TEXT NOT NULL,
            message TEXT NOT NULL,
            created_at TEXT NOT NULL
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS alert_eval_log (
            day TEXT NOT NULL,
            ticker TEXT NOT NULL,
            PRIMARY KEY (day, ticker)
        )"""
    )
    return conn


def _now():
    return datetime.now(timezone.utc).isoformat()


def _today():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


# --------------------------------------------------------------------
# CRUD - called from the Deep Dive "Alert me when..." control and the
# Portfolio page's "My alerts" tab.
# --------------------------------------------------------------------

def count_active(email):
    if not email:
        return 0
    with _conn() as conn:
        n = conn.execute(
            "SELECT COUNT(*) FROM alerts WHERE email = ? AND active = 1",
            (email.strip().lower(),),
        ).fetchone()[0]
    return n


def create_alert(email, ticker, metric, operator, threshold,
                  cooldown_hours=DEFAULT_COOLDOWN_HOURS):
    """Returns {"ok": True, "id": n} or {"ok": False, "error": "..."}.
    Validates metric/operator combination and the per-user active cap
    (MAX_ACTIVE_ALERTS_PER_USER) here so every caller (UI, tests) gets the
    same guarantees without re-checking them itself."""
    if not email or not ticker or not metric:
        return {"ok": False, "error": "Missing email, ticker or metric."}
    email = email.strip().lower()
    ticker = ticker.strip().upper()
    if metric not in ALL_METRICS:
        return {"ok": False, "error": f"Unknown metric '{metric}'."}
    is_categorical = metric in CATEGORICAL_METRICS
    valid_ops = OPERATORS_CATEGORICAL if is_categorical else OPERATORS_NUMERIC
    if operator not in valid_ops:
        return {"ok": False, "error": f"Operator '{operator}' isn't valid for {metric}."}
    if is_categorical:
        allowed = MOAT_STATE_VALUES if metric == "moat_state" else VALUATION_LABEL_VALUES
        if threshold not in allowed:
            return {"ok": False, "error": f"'{threshold}' isn't a valid value for {metric}."}
    else:
        try:
            float(threshold)
        except (TypeError, ValueError):
            return {"ok": False, "error": "Threshold must be a number for this metric."}
    if count_active(email) >= MAX_ACTIVE_ALERTS_PER_USER:
        return {"ok": False,
                "error": f"You've reached the {MAX_ACTIVE_ALERTS_PER_USER}-alert limit. "
                         "Delete or pause one first."}
    with _conn() as conn:
        cur = conn.execute(
            """INSERT INTO alerts
                 (email, ticker, metric, operator, threshold, active,
                  cooldown_hours, created_at)
               VALUES (?, ?, ?, ?, ?, 1, ?, ?)""",
            (email, ticker, metric, operator, str(threshold),
             int(cooldown_hours), _now()),
        )
        alert_id = cur.lastrowid
    return {"ok": True, "id": alert_id}


def _row_to_dict(row):
    return {
        "id": row[0], "email": row[1], "ticker": row[2], "metric": row[3],
        "operator": row[4], "threshold": row[5], "active": bool(row[6]),
        "cooldown_hours": row[7], "created_at": row[8], "last_fired_at": row[9],
    }


_COLS = "id, email, ticker, metric, operator, threshold, active, cooldown_hours, created_at, last_fired_at"


def list_for_user(email):
    """Every alert (active and paused) this user owns, newest first."""
    if not email:
        return []
    with _conn() as conn:
        rows = conn.execute(
            f"SELECT {_COLS} FROM alerts WHERE email = ? ORDER BY id DESC",
            (email.strip().lower(),),
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def list_for_ticker(email, ticker):
    if not email or not ticker:
        return []
    with _conn() as conn:
        rows = conn.execute(
            f"SELECT {_COLS} FROM alerts WHERE email = ? AND ticker = ? ORDER BY id DESC",
            (email.strip().lower(), ticker.strip().upper()),
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def get_alert(alert_id):
    with _conn() as conn:
        row = conn.execute(f"SELECT {_COLS} FROM alerts WHERE id = ?", (alert_id,)).fetchone()
    return _row_to_dict(row) if row else None


def delete_alert(alert_id, email):
    """Ownership-checked delete - a user can only ever delete their own
    alert, never guess another user's id and remove it."""
    if not email:
        return False
    with _conn() as conn:
        cur = conn.execute(
            "DELETE FROM alerts WHERE id = ? AND email = ?",
            (alert_id, email.strip().lower()),
        )
    return cur.rowcount > 0


def set_active(alert_id, email, active):
    if not email:
        return False
    with _conn() as conn:
        cur = conn.execute(
            "UPDATE alerts SET active = ? WHERE id = ? AND email = ?",
            (1 if active else 0, alert_id, email.strip().lower()),
        )
    return cur.rowcount > 0


# --------------------------------------------------------------------
# Nightly evaluation support - called only from alert_engine.py.
# --------------------------------------------------------------------

def active_alerts_for_ticker(ticker):
    if not ticker:
        return []
    with _conn() as conn:
        rows = conn.execute(
            f"SELECT {_COLS} FROM alerts WHERE ticker = ? AND active = 1",
            (ticker.strip().upper(),),
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def tickers_with_active_alerts():
    with _conn() as conn:
        rows = conn.execute("SELECT DISTINCT ticker FROM alerts WHERE active = 1").fetchall()
    return [r[0] for r in rows]


def mark_evaluated_today(ticker, day=None):
    day = day or _today()
    with _conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO alert_eval_log (day, ticker) VALUES (?, ?)",
            (day, ticker.strip().upper()),
        )


def evaluated_today(day=None):
    day = day or _today()
    with _conn() as conn:
        rows = conn.execute("SELECT ticker FROM alert_eval_log WHERE day = ?", (day,)).fetchall()
    return {r[0] for r in rows}


def tickers_needing_extra_pass(cap=200, day=None):
    """Tickers with an active alert that weren't already covered by
    tonight's regular universe scans - the Part 1 spec's "small nightly
    alert-only pass", capped so a huge alert list can never blow the
    nightly budget on its own."""
    have_alerts = set(tickers_with_active_alerts())
    done = evaluated_today(day)
    remaining = sorted(have_alerts - done)
    return remaining[:cap]


def record_fire(alert_id):
    with _conn() as conn:
        conn.execute("UPDATE alerts SET last_fired_at = ? WHERE id = ?", (_now(), alert_id))


def cooldown_ok(alert):
    """True if this alert is allowed to fire again right now (no
    last_fired_at yet, or its cooldown window has fully elapsed)."""
    last = alert.get("last_fired_at")
    if not last:
        return True
    try:
        last_dt = datetime.fromisoformat(last)
    except ValueError:
        return True
    hours = alert.get("cooldown_hours") or DEFAULT_COOLDOWN_HOURS
    return datetime.now(timezone.utc) - last_dt >= timedelta(hours=hours)


def queue_hit(alert_id, email, ticker, message):
    with _conn() as conn:
        conn.execute(
            """INSERT INTO alert_hits_pending (alert_id, email, ticker, message, created_at)
                 VALUES (?, ?, ?, ?, ?)""",
            (alert_id, email, ticker, message, _now()),
        )


def pending_hits_by_email():
    """{email: [{"hit_id","ticker","message"}, ...]} for every queued,
    not-yet-sent hit - the input to alert_engine.send_batched_notifications,
    which sends ONE email + ONE push per email key here regardless of how
    many hits/tickers it contains."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT id, email, ticker, message FROM alert_hits_pending ORDER BY email, id"
        ).fetchall()
    out = {}
    for hit_id, email, ticker, message in rows:
        out.setdefault(email, []).append(
            {"hit_id": hit_id, "ticker": ticker, "message": message}
        )
    return out


def clear_pending_hits(hit_ids):
    if not hit_ids:
        return
    with _conn() as conn:
        conn.executemany("DELETE FROM alert_hits_pending WHERE id = ?", [(i,) for i in hit_ids])


def stats():
    """Admin-facing aggregate only (no per-user list) - same convention as
    follow_store.follow_count()."""
    with _conn() as conn:
        users = conn.execute("SELECT COUNT(DISTINCT email) FROM alerts WHERE active = 1").fetchone()[0]
        active = conn.execute("SELECT COUNT(*) FROM alerts WHERE active = 1").fetchone()[0]
        pending = conn.execute("SELECT COUNT(*) FROM alert_hits_pending").fetchone()[0]
    return {"users_with_alerts": users, "active_alerts": active, "pending_hits": pending}
