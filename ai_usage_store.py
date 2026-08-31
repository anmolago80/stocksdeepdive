"""
ai_usage_store.py

Per-call usage log for every AI feature in the roadmap
(AI_ROADMAP_stocksdeepdive.md) - one row per SUCCESSFUL Anthropic API
call: who, which feature, which model, the real token counts, and the
estimated USD cost (ai_client.estimate_cost_usd). This is both the
quota counter (questions_today/questions_this_month, read by
ai_gate.check()) and the spend meter (spend_this_month, read by the
admin AI settings panel) - one source of truth for both, so a quota
enforcement bug and a spend-meter display bug can never quietly
disagree with each other.

Deliberately does NOT store the question text or the model's answer -
only who/what/how-much. Nothing here is needed to enforce a quota or
show a spend total, and not storing it means a private My Portfolio
question can never leak into an admin-visible table the way a raw
prompt log would.

Only a call that actually succeeded is ever recorded here - a call
ai_gate.check() blocked, or one ai_client.ask() itself failed, costs
nothing and must never count against anyone's quota. Record it AFTER
the Anthropic call returns, using its real usage.input_tokens/
output_tokens - never an estimate made before the call, since the
actual token counts (and therefore actual cost) aren't known until then.

Same SQLite file / volume-resolution rule as every other store here -
see positions_store.py's docstring.
"""

import os
import sqlite3
from datetime import datetime, timezone


def _data_dir():
    return os.environ.get("RAILWAY_VOLUME_MOUNT_PATH") or os.path.dirname(__file__)


DB_PATH = os.path.join(_data_dir(), "stocksdeepdive.db")


def _conn():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS ai_usage (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT NOT NULL,
            feature TEXT NOT NULL,
            model TEXT NOT NULL,
            input_tokens INTEGER NOT NULL,
            output_tokens INTEGER NOT NULL,
            cost_usd REAL NOT NULL,
            created_at TEXT NOT NULL
        )"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_ai_usage_email_created "
        "ON ai_usage (email, created_at)"
    )
    return conn


def _now():
    return datetime.now(timezone.utc)


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


def record_usage(email, feature, model, input_tokens, output_tokens, cost_usd):
    email = (email or "").strip().lower()
    if not email:
        return
    with _conn() as conn:
        conn.execute(
            """INSERT INTO ai_usage
                 (email, feature, model, input_tokens, output_tokens,
                  cost_usd, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (email, feature, model, int(input_tokens or 0),
             int(output_tokens or 0), float(cost_usd or 0.0), _iso(_now())),
        )


def _day_start_iso():
    n = _now()
    return _iso(n.replace(hour=0, minute=0, second=0, microsecond=0))


def _month_start_iso():
    n = _now()
    return _iso(n.replace(day=1, hour=0, minute=0, second=0, microsecond=0))


def questions_today(email):
    email = (email or "").strip().lower()
    if not email:
        return 0
    with _conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM ai_usage WHERE email = ? AND created_at >= ?",
            (email, _day_start_iso()),
        ).fetchone()
    return row[0] if row else 0


def questions_this_month(email):
    email = (email or "").strip().lower()
    if not email:
        return 0
    with _conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM ai_usage WHERE email = ? AND created_at >= ?",
            (email, _month_start_iso()),
        ).fetchone()
    return row[0] if row else 0


def spend_this_month():
    """SUM(cost_usd) across EVERY user this calendar month (UTC) - the
    number ai_gate.check() compares against the site-wide spend cap, and
    the headline figure on the admin panel's live spend meter."""
    with _conn() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(cost_usd), 0) FROM ai_usage WHERE created_at >= ?",
            (_month_start_iso(),),
        ).fetchone()
    return float(row[0]) if row else 0.0


def spend_today():
    with _conn() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(cost_usd), 0) FROM ai_usage WHERE created_at >= ?",
            (_day_start_iso(),),
        ).fetchone()
    return float(row[0]) if row else 0.0


def usage_by_feature_this_month():
    """{feature: {"count": n, "cost_usd": x}} for this calendar month -
    the admin panel's per-feature spend breakdown."""
    with _conn() as conn:
        rows = conn.execute(
            """SELECT feature, COUNT(*), COALESCE(SUM(cost_usd), 0)
               FROM ai_usage WHERE created_at >= ?
               GROUP BY feature ORDER BY 3 DESC""",
            (_month_start_iso(),),
        ).fetchall()
    return {r[0]: {"count": r[1], "cost_usd": float(r[2])} for r in rows}


def distinct_users_this_month():
    with _conn() as conn:
        row = conn.execute(
            "SELECT COUNT(DISTINCT email) FROM ai_usage WHERE created_at >= ?",
            (_month_start_iso(),),
        ).fetchone()
    return row[0] if row else 0
