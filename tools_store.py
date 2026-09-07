"""
tools_store.py

Private per-user data for the 🧰 Tools hub (mega-batch Part 18 onward:
the Budget Planner's one saved plan today; Part 19's saved utility-bill
checks and Part 20's saved cash-vs-offset scenarios are meant to land as
new tables in this SAME module later - "a new entry in a list, not a
rebuild", the spec's own words for the tools REGISTRY, applied here too
at the persistence layer so every Tools sub-feature shares one file
instead of each growing its own store module).

Part 22 (🛡️ Insurance bill check) goes further than "a new table" - it
adds NO new table for its saved checks at all, deliberately reusing
bill_checks/bill_check_history/bill_check_usage AS-IS (an insurance
check is just a row with fuel="ins_health"/"ins_car"/"ins_home"/
"ins_ctp" - "they are bills", per that part's own spec wording). This
is also how the two tools end up sharing the SAME trial/cap pool with
ZERO changes to can_check()/record_check_usage()/bill_check_usage_
status() below: those were already written keyed on email alone, never
on which tool or fuel is asking.

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
    # Part 20 (Cash vs Offset vs Borrow): UNLIKE budget_plans above, this
    # is never auto-saved - the spec's own words are "nothing saved
    # unless signed in and the user saves the scenario". inputs_json
    # holds the whole input card verbatim (cash/rates/splits/country/
    # etc.) so reloading a saved scenario reproduces its exact scenario
    # cards/charts, not just a subset of fields.
    conn.execute(
        """CREATE TABLE IF NOT EXISTS debt_recycling_scenarios (
            email TEXT NOT NULL PRIMARY KEY,
            inputs_json TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )"""
    )
    # Part 19 (Utilities bill check): the trial/cap counter - ONE row per
    # email, lifetime. trial_used flips 0->1 the first time a check is
    # ever recorded and never resets. month_key/month_count track the
    # SEPARATE 10-checks-per-calendar-month cap that only ever applies to
    # an active subscriber (is_subscribed()) - a signed-in-but-unsub'd
    # visitor who has already used their trial is blocked outright
    # (see can_check() below), so month_count only ever increments for
    # subscribers.
    conn.execute(
        """CREATE TABLE IF NOT EXISTS bill_check_usage (
            email TEXT NOT NULL PRIMARY KEY,
            trial_used INTEGER NOT NULL DEFAULT 0,
            month_key TEXT,
            month_count INTEGER NOT NULL DEFAULT 0
        )"""
    )
    # One saved bill check per (email, fuel, postcode) - a rescan of the
    # SAME fuel+postcode UPDATES this row (spec: "Rescanning the same
    # fuel+postcode updates the row and keeps the prior reading as
    # history"), a different fuel or postcode is a separate row/card on
    # the dashboard. extra_json carries whatever fields don't have their
    # own column (retailer/plan_name text, benchmark-specific numbers) -
    # a few hundred bytes per check per the spec's own storage note.
    # NEVER a column or key here for the image itself - it is never
    # written to disk at all (see bill_check_engine.py's own docstring).
    conn.execute(
        """CREATE TABLE IF NOT EXISTS bill_checks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT NOT NULL,
            fuel TEXT NOT NULL,
            postcode TEXT,
            status TEXT NOT NULL,
            annual_cost REAL,
            cheapest_annual_cost REAL,
            switchable_saving REAL,
            extra_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(email, fuel, postcode)
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS bill_check_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            bill_id INTEGER NOT NULL,
            plan_name TEXT,
            annual_cost REAL,
            recorded_at TEXT NOT NULL
        )"""
    )
    # Part 22 (Insurance bill check): renewal-creep tracking ("your
    # premium rose 18% - sums insured rose 3%") needs more than just the
    # PRIOR annual_cost this table already kept - it needs that prior
    # save's whole extra blob (sum_insured, tier, cover_type, ...) so a
    # later rescan can read back whichever secondary field matters for
    # THAT policy type. Guarded ALTER TABLE, same belt-and-braces
    # pattern portfolio_store.py/blog_store.py already use to evolve an
    # already-deployed sqlite schema (CREATE TABLE IF NOT EXISTS above
    # is a no-op against an existing table). Utilities rows simply never
    # populate this column (still NULL, harmless) - only insurance's own
    # renewal_creep() read path looks at it.
    try:
        conn.execute("ALTER TABLE bill_check_history ADD COLUMN extra_json TEXT")
    except sqlite3.OperationalError:
        pass
    # Admin-editable "typical deal" benchmark figures for the manual-
    # entry-only rows (internet/mobile - spec's own wording) - a plain
    # key -> typical annual cost table, owner-maintained via the Tools
    # admin panel rather than hardcoded in bill_check_engine.py, so the
    # owner can update a stale figure without a redeploy.
    conn.execute(
        """CREATE TABLE IF NOT EXISTS typical_deal_rates (
            service_key TEXT NOT NULL PRIMARY KEY,
            typical_annual_cost REAL NOT NULL,
            updated_at TEXT NOT NULL
        )"""
    )
    return conn


# -----------------------------------------------------------------
# Part 19: trial/cap gating. Deliberately mirrors is_subscribed()'s own
# fail-CLOSED philosophy (paywall_engine.py's module docstring) - any
# read error here must deny a check, never silently allow one, since
# every allowed check costs real Anthropic/AI spend.
# -----------------------------------------------------------------

BILL_CHECK_MONTHLY_CAP = 10


def bill_check_usage_status(email):
    """{"trial_used": bool, "month_count": int, "month_cap": int} for the
    "7 of 10 checks left this month" UI caption - informational only,
    never the allow/deny decision itself (can_check() below owns that,
    same split as ai_gate.check() vs ai_gate.remaining())."""
    if not email:
        return {"trial_used": False, "month_count": 0, "month_cap": BILL_CHECK_MONTHLY_CAP}
    now_month = datetime.now(timezone.utc).strftime("%Y-%m")
    with _conn() as conn:
        row = conn.execute(
            "SELECT trial_used, month_key, month_count FROM bill_check_usage WHERE email = ?",
            (email,),
        ).fetchone()
    if not row:
        return {"trial_used": False, "month_count": 0, "month_cap": BILL_CHECK_MONTHLY_CAP}
    trial_used, month_key, month_count = row
    if month_key != now_month:
        month_count = 0
    return {"trial_used": bool(trial_used), "month_count": month_count,
            "month_cap": BILL_CHECK_MONTHLY_CAP}


def can_check(email, paywall_enabled, is_subscribed):
    """(allowed: bool, reason: str). reason is one of "trial" (this
    check consumes the free lifetime trial), "subscribed" (within the
    monthly cap), "trial_used_paywall_off" (owner's own fail-closed
    rule: "while PAYWALL_ENABLED is off, past-trial visitors see a
    blocked card - protecting AI spend"), "needs_subscription", or
    "monthly_cap_reached". Takes paywall_enabled/is_subscribed as
    plain bool ARGUMENTS rather than importing paywall_engine directly -
    keeps this module's only external dependency the stdlib, matching
    every other *_store.py in this codebase, and makes the gating logic
    trivially unit-testable without Streamlit or Stripe in the loop."""
    if not email:
        return False, "not_signed_in"
    status = bill_check_usage_status(email)
    if not status["trial_used"]:
        return True, "trial"
    if not paywall_enabled:
        return False, "trial_used_paywall_off"
    if not is_subscribed:
        return False, "needs_subscription"
    if status["month_count"] >= status["month_cap"]:
        return False, "monthly_cap_reached"
    return True, "subscribed"


def record_check_usage(email):
    """Call ONLY after a check the caller's can_check() already allowed
    actually ran (mirrors ai_gate.record()'s own "only after real work
    happened" convention). Flips trial_used on the very first call for
    an email; every call after that increments month_count, resetting
    it to 1 (not 0) on a new calendar month rather than 0 then needing a
    second increment."""
    if not email:
        return
    now_month = datetime.now(timezone.utc).strftime("%Y-%m")
    with _conn() as conn:
        row = conn.execute(
            "SELECT trial_used, month_key, month_count FROM bill_check_usage WHERE email = ?",
            (email,),
        ).fetchone()
        if not row:
            conn.execute(
                "INSERT INTO bill_check_usage (email, trial_used, month_key, month_count) "
                "VALUES (?, 0, ?, 0)", (email, now_month),
            )
            row = (0, now_month, 0)
        trial_used, month_key, month_count = row
        if not trial_used:
            conn.execute(
                "UPDATE bill_check_usage SET trial_used = 1 WHERE email = ?", (email,),
            )
            return
        new_count = (month_count + 1) if month_key == now_month else 1
        conn.execute(
            "UPDATE bill_check_usage SET month_key = ?, month_count = ? WHERE email = ?",
            (now_month, new_count, email),
        )


# -----------------------------------------------------------------
# Part 19: saved bill checks + rescan history. Storage note (spec):
# "extracted fields + comparison results only ... a few hundred bytes
# per check" and "No images, ever" - enforced by construction here
# simply by there being no column any caller could put image bytes into.
# -----------------------------------------------------------------

def list_bill_checks(email):
    """Every saved check for this email, most-recent-saving first (the
    dashboard's own sort is by biggest saving, done by the caller - this
    is just the raw rows)."""
    if not email:
        return []
    with _conn() as conn:
        rows = conn.execute(
            "SELECT id, fuel, postcode, status, annual_cost, cheapest_annual_cost, "
            "switchable_saving, extra_json, created_at, updated_at FROM bill_checks "
            "WHERE email = ? ORDER BY updated_at DESC",
            (email,),
        ).fetchall()
    out = []
    for r in rows:
        try:
            extra = json.loads(r[7]) if r[7] else {}
        except (TypeError, ValueError):
            extra = {}
        out.append({
            "id": r[0], "fuel": r[1], "postcode": r[2], "status": r[3],
            "annual_cost": r[4], "cheapest_annual_cost": r[5],
            "switchable_saving": r[6], "extra": extra,
            "created_at": r[8], "updated_at": r[9],
        })
    return out


def save_bill_check(email, fuel, postcode, status, annual_cost,
                    cheapest_annual_cost, switchable_saving, extra):
    """Upserts on (email, fuel, postcode) - a rescan of the same
    fuel+postcode updates the row and, per the spec, appends the PRIOR
    reading to bill_check_history first (so "was $1,393 last yr" has
    something to read from) rather than silently discarding it. extra:
    plain dict of whatever non-columned fields this fuel/status needs
    (retailer, plan_name, rank/total_count, benchmark typical figures,
    etc.) - json-serialised as-is, caller owns its shape."""
    if not email:
        return None
    now = datetime.now(timezone.utc).isoformat()
    extra_json = json.dumps(extra or {})
    with _conn() as conn:
        existing = conn.execute(
            "SELECT id, annual_cost, extra_json FROM bill_checks "
            "WHERE email = ? AND fuel = ? AND postcode IS ?",
            (email, fuel, postcode),
        ).fetchone()
        if existing:
            bill_id, prev_annual_cost, prev_extra_json = existing
            try:
                prev_plan_name = (json.loads(prev_extra_json) if prev_extra_json else {}).get("plan_name")
            except (TypeError, ValueError):
                prev_plan_name = None
            conn.execute(
                "INSERT INTO bill_check_history (bill_id, plan_name, annual_cost, recorded_at, extra_json) "
                "VALUES (?, ?, ?, ?, ?)",
                (bill_id, prev_plan_name, prev_annual_cost, now, prev_extra_json),
            )
            conn.execute(
                "UPDATE bill_checks SET status = ?, annual_cost = ?, "
                "cheapest_annual_cost = ?, switchable_saving = ?, extra_json = ?, "
                "updated_at = ? WHERE id = ?",
                (status, annual_cost, cheapest_annual_cost, switchable_saving,
                 extra_json, now, bill_id),
            )
            return bill_id
        cur = conn.execute(
            "INSERT INTO bill_checks (email, fuel, postcode, status, annual_cost, "
            "cheapest_annual_cost, switchable_saving, extra_json, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (email, fuel, postcode, status, annual_cost, cheapest_annual_cost,
             switchable_saving, extra_json, now, now),
        )
        return cur.lastrowid


def get_bill_check_history(bill_id):
    """[{"plan_name", "annual_cost", "recorded_at", "extra"}, ...]
    oldest first, for the small per-row history table the spec asks
    for. "extra" (Part 22) is the prior save's full extra dict ({} for
    a pre-Part-22 history row that predates the extra_json column, or
    any row where it was never set) - insurance's renewal_creep() reads
    hist[-1]["extra"] for the sum-insured/tier comparison; Utilities'
    own history rendering simply never looks at this key."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT plan_name, annual_cost, recorded_at, extra_json FROM bill_check_history "
            "WHERE bill_id = ? ORDER BY recorded_at ASC",
            (bill_id,),
        ).fetchall()
    out = []
    for r in rows:
        try:
            extra = json.loads(r[3]) if r[3] else {}
        except (TypeError, ValueError):
            extra = {}
        out.append({"plan_name": r[0], "annual_cost": r[1], "recorded_at": r[2], "extra": extra})
    return out


def delete_bill_check(email, bill_id):
    """Lets a signed-in visitor remove one saved check (not in the
    original mock, but a reasonable, low-risk affordance for private
    per-user data with no downstream reference to a row's id anywhere
    else) - scoped to `email` so one visitor can never delete another's
    row even if an id were guessed."""
    if not email or not bill_id:
        return
    with _conn() as conn:
        conn.execute("DELETE FROM bill_check_history WHERE bill_id = ? AND bill_id IN "
                     "(SELECT id FROM bill_checks WHERE id = ? AND email = ?)",
                     (bill_id, bill_id, email))
        conn.execute("DELETE FROM bill_checks WHERE id = ? AND email = ?", (bill_id, email))


# -----------------------------------------------------------------
# Part 19: admin-editable "typical deal" benchmark table (internet/
# mobile manual-entry rows). Owner-maintained via the Tools admin panel.
#
# Part 22 (Insurance bill check) reuses this SAME table for its own
# car/home/CTP "typical premium" benchmark, per state/policy-type band -
# "reuse machinery wherever it exists" rather than a second table with
# an identical shape. service_key for an insurance row is a compound
# "ins_<policy_type>:<state>" string (e.g. "ins_car:NSW", "ins_home:VIC")
# rather than a plain "internet_au"-style key - get_typical_deal_rates()/
# set_typical_deal_rate() themselves need no change at all, since
# service_key was always an opaque caller-chosen string.
# -----------------------------------------------------------------

def get_typical_deal_rates():
    """{service_key: typical_annual_cost} for every configured row -
    empty dict (not an error) if none have ever been set, so a caller
    can safely .get(key) with its own fallback default."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT service_key, typical_annual_cost FROM typical_deal_rates",
        ).fetchall()
    return {r[0]: r[1] for r in rows}


def set_typical_deal_rate(service_key, typical_annual_cost):
    if not service_key:
        return
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as conn:
        conn.execute(
            "INSERT INTO typical_deal_rates (service_key, typical_annual_cost, updated_at) "
            "VALUES (?, ?, ?) ON CONFLICT(service_key) DO UPDATE SET "
            "typical_annual_cost = excluded.typical_annual_cost, updated_at = excluded.updated_at",
            (service_key, typical_annual_cost, now),
        )


def get_debt_recycling_scenario(email):
    """{"inputs": {...}} or None if this email has never saved a
    scenario. inputs is exactly the dict save_debt_recycling_scenario()
    was last called with - the caller (app.py) owns its own shape."""
    if not email:
        return None
    with _conn() as conn:
        row = conn.execute(
            "SELECT inputs_json FROM debt_recycling_scenarios WHERE email = ?",
            (email,),
        ).fetchone()
    if not row:
        return None
    try:
        inputs = json.loads(row[0]) if row[0] else {}
    except (TypeError, ValueError):
        inputs = {}
    return {"inputs": inputs}


def save_debt_recycling_scenario(email, inputs):
    """Upserts the ONE scenario this email is allowed, same one-per-user
    contract as save_budget_plan(). Only ever called from an explicit
    "Save this scenario" button click (app.py) - never from a plain page
    render, per the spec's own privacy rule for this tool."""
    if not email:
        return
    now = datetime.now(timezone.utc).isoformat()
    inputs_json = json.dumps(inputs or {})
    with _conn() as conn:
        conn.execute(
            "INSERT INTO debt_recycling_scenarios (email, inputs_json, updated_at) "
            "VALUES (?, ?, ?) ON CONFLICT(email) DO UPDATE SET "
            "inputs_json = excluded.inputs_json, updated_at = excluded.updated_at",
            (email, inputs_json, now),
        )


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
