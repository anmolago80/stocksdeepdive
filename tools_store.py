"""
tools_store.py

Private per-user data for the 🧰 Tools hub (mega-batch Part 18 onward:
the Budget Planner's one saved plan today; Part 19's saved utility-bill
checks and Part 20's saved cash-vs-offset scenarios are meant to land as
new tables in this SAME module later - "a new entry in a list, not a
rebuild", the spec's own words for the tools REGISTRY, applied here too
at the persistence layer so every Tools sub-feature shares one file
instead of each growing its own store module).

WHERE THE FILE LIVES / WHY SQLITE: identical rule to every other store in
this app (see portfolio_store.py's own docstring) - the attached Railway
Volume when one exists, falling back to this directory locally. Same
physical stocksdeepdive.db file portfolio_store.py already uses (a
second sqlite file would just be more moving parts for no benefit) -
tables are namespaced by name (budget_plans), not by a separate database.

IDENTITY: the signed-in email from paywall_engine.current_user_email() -
this module never sees or stores anything else about who the user is,
and every read/write below takes `email` as its first argument and
scopes its query to exactly that value.

PRIVACY: everything in here is private user data per the mega-batch's
own standing rule - never surfaced through the public API, MCP, nightly
snapshots, or any signed-out view. The Budget Planner amendment (owner,
6 Sep) requires sign-in for all of Tools, so there is no "anonymous
plan" state to worry about here at all - a plan only ever exists once
an email is attached to it."""

import json
import os
import sqlite3
from datetime import datetime, timezone


def _data_dir():
    return os.environ.get("RAILWAY_VOLUME_MOUNT_PATH") or os.path.dirname(__file__)


DB_PATH = os.path.join(_data_dir(), "stocksdeepdive.db")


def _conn():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS budget_plans (
            email TEXT NOT NULL PRIMARY KEY,
            money_in REAL,
            categories_json TEXT NOT NULL,
            country TEXT,
            years INTEGER,
            updated_at TEXT NOT NULL
        )"""
    )
    return conn


def get_budget_plan(email):
    """{"money_in": float|None, "categories": {id: float}, "country":
    str|None, "years": int|None} or None if this email has never saved
    a plan. categories only ever contains keys the plan actually had a
    value for (never a full zero-filled dict) - an unfilled category on
    the page stays unfilled on reload too."""
    if not email:
        return None
    with _conn() as conn:
        row = conn.execute(
            "SELECT money_in, categories_json, country, years "
            "FROM budget_plans WHERE email = ?",
            (email,),
        ).fetchone()
    if not row:
        return None
    try:
        categories = json.loads(row[1]) if row[1] else {}
    except (TypeError, ValueError):
        categories = {}
    return {"money_in": row[0], "categories": categories, "country": row[2], "years": row[3]}


def save_budget_plan(email, money_in, categories, country=None, years=None):
    """Upserts the ONE plan this email is allowed (spec: "signed-in users
    can save exactly one plan"). categories is stored as-is (only the
    filled keys the caller passes) - the caller (app.py) is responsible
    for not passing None/blank entries it doesn't want persisted."""
    if not email:
        return
    now = datetime.now(timezone.utc).isoformat()
    categories_json = json.dumps(categories or {})
    with _conn() as conn:
        conn.execute(
            "INSERT INTO budget_plans (email, money_in, categories_json, country, years, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(email) DO UPDATE SET "
            "money_in = excluded.money_in, categories_json = excluded.categories_json, "
            "country = excluded.country, years = excluded.years, updated_at = excluded.updated_at",
            (email, money_in, categories_json, country, years, now),
        )
