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

# Part 30 (8 Sep 2026, owner's own request: "give option with different
# budget and debt recycling names just like my portfolio"): the name a
# pre-existing single saved plan/scenario is folded into the first time
# an already-live email hits the new multi-named-plan schema below (see
# _migrate_legacy_single_plan_schema) - chosen per the owner's own
# answer ("Auto-migrate into a first named plan") rather than starting
# every existing user empty.
_DEFAULT_BUDGET_PLAN_NAME = "My Plan"
_DEFAULT_DEBT_RECYCLING_NAME = "My Scenario"
# Part 42 (13 Sep 2026): Super & Retirement projector. Same named-
# scenario shape as debt recycling above (names registry + inputs_json
# blob table) even though v1's own render function only ever uses this
# ONE default name (no scenario switcher in the mock) - so a future
# switcher needs zero migration, exactly the position debt_recycling was
# in before its own Part 30 multi-scenario upgrade.
_DEFAULT_SUPER_SCENARIO_NAME = "My Projection"


def _table_columns(conn, table):
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]


def _migrate_legacy_single_plan_schema(conn):
    """One-time, idempotent fold-up from the original one-plan-per-email
    shape into the multi-named-plan one (Part 30). Mirrors
    portfolio_store.py's own _migrate_legacy_schema exactly: rename the
    old table aside, recreate it with the new (email, name) PK, and give
    every pre-existing row the same fixed name so nothing already saved
    disappears or has to be re-entered - it just becomes named-plan #1.
    Runs on every connection but is a single cheap PRAGMA check once
    already migrated, so it costs nothing after the first call
    post-upgrade. A brand-new install never has a legacy table to find
    here at all - CREATE TABLE IF NOT EXISTS below already creates the
    new (email, name) shape directly, so `name` is present from the
    start and this whole function no-ops on its very first PRAGMA
    check."""
    _budget_cols = _table_columns(conn, "budget_plans")
    if "name" not in _budget_cols:
        conn.execute("ALTER TABLE budget_plans RENAME TO budget_plans_pre_multi")
        conn.execute(
            """CREATE TABLE budget_plans (
                email TEXT NOT NULL,
                name TEXT NOT NULL,
                money_in REAL,
                categories_json TEXT NOT NULL,
                country TEXT,
                years INTEGER,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (email, name)
            )"""
        )
        _now = datetime.now(timezone.utc).isoformat()
        for row in conn.execute(
            "SELECT email, money_in, categories_json, country, years, updated_at "
            "FROM budget_plans_pre_multi"
        ).fetchall():
            _email, _money_in, _categories_json, _country, _years, _updated_at = row
            conn.execute(
                "INSERT OR IGNORE INTO budget_plan_names (email, name, created_at) VALUES (?, ?, ?)",
                (_email, _DEFAULT_BUDGET_PLAN_NAME, _now),
            )
            conn.execute(
                "INSERT OR IGNORE INTO budget_plans "
                "(email, name, money_in, categories_json, country, years, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (_email, _DEFAULT_BUDGET_PLAN_NAME, _money_in, _categories_json,
                 _country, _years, _updated_at),
            )
        conn.execute("DROP TABLE budget_plans_pre_multi")

    _dr_cols = _table_columns(conn, "debt_recycling_scenarios")
    if "name" not in _dr_cols:
        conn.execute(
            "ALTER TABLE debt_recycling_scenarios RENAME TO debt_recycling_scenarios_pre_multi"
        )
        conn.execute(
            """CREATE TABLE debt_recycling_scenarios (
                email TEXT NOT NULL,
                name TEXT NOT NULL,
                inputs_json TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (email, name)
            )"""
        )
        _now = datetime.now(timezone.utc).isoformat()
        for row in conn.execute(
            "SELECT email, inputs_json, updated_at FROM debt_recycling_scenarios_pre_multi"
        ).fetchall():
            _email, _inputs_json, _updated_at = row
            conn.execute(
                "INSERT OR IGNORE INTO debt_recycling_scenario_names (email, name, created_at) "
                "VALUES (?, ?, ?)",
                (_email, _DEFAULT_DEBT_RECYCLING_NAME, _now),
            )
            conn.execute(
                "INSERT OR IGNORE INTO debt_recycling_scenarios "
                "(email, name, inputs_json, updated_at) VALUES (?, ?, ?, ?)",
                (_email, _DEFAULT_DEBT_RECYCLING_NAME, _inputs_json, _updated_at),
            )
        conn.execute("DROP TABLE debt_recycling_scenarios_pre_multi")


def _conn():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    # Part 30: name registries, one row per named plan/scenario this
    # email has - mirrors portfolio_store.py's own `portfolios` table
    # exactly (email, name, created_at, PK (email, name)). A name can
    # exist here before any plan/scenario data has ever been saved
    # under it (e.g. right after "Create", or for Debt Recycling before
    # its own explicit save button is ever clicked) - same as an empty
    # portfolio existing before its first holding.
    conn.execute(
        """CREATE TABLE IF NOT EXISTS budget_plan_names (
            email TEXT NOT NULL,
            name TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (email, name)
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS debt_recycling_scenario_names (
            email TEXT NOT NULL,
            name TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (email, name)
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS budget_plans (
            email TEXT NOT NULL,
            name TEXT NOT NULL,
            money_in REAL,
            categories_json TEXT NOT NULL,
            country TEXT,
            years INTEGER,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (email, name)
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
            email TEXT NOT NULL,
            name TEXT NOT NULL,
            inputs_json TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (email, name)
        )"""
    )
    # Part 42 (13 Sep 2026): Super & Retirement projector's own saved-
    # inputs table, IDENTICAL shape to debt_recycling_scenario_names /
    # debt_recycling_scenarios above (names registry + inputs_json blob,
    # both keyed on (email, name)) - see _DEFAULT_SUPER_SCENARIO_NAME's
    # own comment for why a full names table exists even though v1 only
    # ever uses one name.
    conn.execute(
        """CREATE TABLE IF NOT EXISTS super_scenario_names (
            email TEXT NOT NULL,
            name TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (email, name)
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS super_scenarios (
            email TEXT NOT NULL,
            name TEXT NOT NULL,
            inputs_json TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (email, name)
        )"""
    )
    _migrate_legacy_single_plan_schema(conn)
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


# 9 Sep 2026 fix: the Insurance car/home/CTP "typical premium" benchmark
# used to require the owner to type EVERY figure in by hand before it
# showed anything but "no typical premium set yet" - an empty admin table
# out of the box makes the whole benchmark card useless on day one. This
# seeds real, published reference figures so the benchmark actually says
# something from the first deploy, while staying strictly additive:
# INSERT OR IGNORE only ever fills a row that has NEVER been set (missing
# from the table entirely) - it can never overwrite a value the owner
# later edits via the admin panel, even a deliberately-zeroed one, and is
# safe to call on every page_tools() render (a no-op after the first).
#
# Sources (checked live, not from training-data memory - see this
# session's own research): Canstar's "How much does car insurance cost in
# Australia?" (canstar.com.au, updated 10 Apr 2026, state averages from
# 27 May 2025 data) for car; Canstar's "How Much is Home and Contents
# Insurance in Australia?" (canstar.com.au, updated 27 Aug 2026, 2026
# Home & Contents Insurance Awards data) for home - combined
# home+contents figure, standard (not cyclone-zone North QLD) rate used
# for QLD. A second source (Finder, early 2025) gave meaningfully LOWER
# car figures for the same states (e.g. QLD $1,274 vs Canstar's $2,010) -
# flagged to the owner rather than silently reconciled; Canstar's more
# recent figures were used. ACT has no published figure from either
# source for car or home - deliberately left UNSET (never guessed) rather
# than reusing another state's number or a fabricated average; the
# benchmark card will keep showing "not available yet" for ACT car/home
# until a real figure is sourced or the owner sets one manually. CTP is
# not seeded at all - no publisher was found with a clean, current,
# state-by-state CTP premium table (schemes vary too much by state to
# safely average), so every CTP benchmark keeps showing "not available
# yet" until the owner has real figures to enter.
DEFAULT_TYPICAL_INSURANCE_PREMIUMS = {
    "ins_car:NSW": 2570.0, "ins_car:VIC": 2940.0, "ins_car:QLD": 2010.0,
    "ins_car:WA": 2032.0, "ins_car:SA": 1970.0, "ins_car:TAS": 1785.0,
    "ins_car:NT": 2283.0,
    "ins_home:NSW": 2805.0, "ins_home:VIC": 2425.0, "ins_home:QLD": 3362.0,
    "ins_home:WA": 2318.0, "ins_home:SA": 2061.0, "ins_home:TAS": 2303.0,
    "ins_home:NT": 5340.0,
}


def seed_default_typical_deal_rates():
    """Idempotent, additive-only: inserts DEFAULT_TYPICAL_INSURANCE_
    PREMIUMS for any service_key that has NEVER been set, and touches
    nothing that already has a row (own edit or a prior seed run alike).
    Safe to call on every page_tools() render."""
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as conn:
        conn.executemany(
            "INSERT OR IGNORE INTO typical_deal_rates "
            "(service_key, typical_annual_cost, updated_at) VALUES (?, ?, ?)",
            [(k, v, now) for k, v in DEFAULT_TYPICAL_INSURANCE_PREMIUMS.items()],
        )


def list_debt_recycling_scenario_names(email):
    """Ordered names of every Debt Recycling scenario this email has
    (oldest first) - empty list if none yet. Mirrors portfolio_store.
    list_portfolios() (Part 30, 8 Sep 2026)."""
    if not email:
        return []
    with _conn() as conn:
        rows = conn.execute(
            "SELECT name FROM debt_recycling_scenario_names WHERE email = ? ORDER BY created_at",
            (email,),
        ).fetchall()
    return [r[0] for r in rows]


def create_debt_recycling_scenario(email, name):
    """Registers a new, empty named scenario - mirrors portfolio_store.
    create_portfolio(). Idempotent (INSERT OR IGNORE): calling it again
    for a name that already exists is a harmless no-op, never an
    error."""
    if not email or not name:
        return
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO debt_recycling_scenario_names (email, name, created_at) "
            "VALUES (?, ?, ?)",
            (email, name, now),
        )


def rename_debt_recycling_scenario(email, old_name, new_name):
    if not email or not old_name or not new_name or old_name == new_name:
        return
    with _conn() as conn:
        conn.execute(
            "UPDATE debt_recycling_scenario_names SET name = ? WHERE email = ? AND name = ?",
            (new_name, email, old_name),
        )
        conn.execute(
            "UPDATE debt_recycling_scenarios SET name = ? WHERE email = ? AND name = ?",
            (new_name, email, old_name),
        )


def delete_debt_recycling_scenario(email, name):
    if not email or not name:
        return
    with _conn() as conn:
        conn.execute(
            "DELETE FROM debt_recycling_scenario_names WHERE email = ? AND name = ?",
            (email, name),
        )
        conn.execute(
            "DELETE FROM debt_recycling_scenarios WHERE email = ? AND name = ?",
            (email, name),
        )


def ensure_default_debt_recycling_scenario(email):
    """Guarantees at least one named scenario exists for a signed-in
    email - same role as portfolio_store.ensure_default_portfolio().
    Called once at the top of the Debt Recycling render so a brand-new
    user (never migrated, never created one) always has something to
    select."""
    if not email:
        return
    if not list_debt_recycling_scenario_names(email):
        create_debt_recycling_scenario(email, _DEFAULT_DEBT_RECYCLING_NAME)


def get_debt_recycling_scenario(email, name):
    """{"inputs": {...}} or None if this email has never saved this
    named scenario. inputs is exactly the dict save_debt_recycling_
    scenario() was last called with for this name - the caller (app.py)
    owns its own shape."""
    if not email or not name:
        return None
    with _conn() as conn:
        row = conn.execute(
            "SELECT inputs_json FROM debt_recycling_scenarios WHERE email = ? AND name = ?",
            (email, name),
        ).fetchone()
    if not row:
        return None
    try:
        inputs = json.loads(row[0]) if row[0] else {}
    except (TypeError, ValueError):
        inputs = {}
    return {"inputs": inputs}


def save_debt_recycling_scenario(email, name, inputs):
    """Upserts this ONE named scenario (Part 30 - previously the one
    scenario this email was allowed at all; multiple names now live
    side by side, same one-row-per-name contract as save_budget_plan()).
    Only ever called from an explicit "Save this scenario" button click
    (app.py) - never from a plain page render, per the spec's own
    privacy rule for this tool. Also registers `name` in the names
    registry (INSERT OR IGNORE) as a defensive belt-and-braces - every
    caller is expected to have already called create_debt_recycling_
    scenario()/ensure_default_debt_recycling_scenario() first, but a
    save must never silently write an orphan row the switcher can't
    see."""
    if not email or not name:
        return
    now = datetime.now(timezone.utc).isoformat()
    inputs_json = json.dumps(inputs or {})
    with _conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO debt_recycling_scenario_names (email, name, created_at) "
            "VALUES (?, ?, ?)",
            (email, name, now),
        )
        conn.execute(
            "INSERT INTO debt_recycling_scenarios (email, name, inputs_json, updated_at) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(email, name) DO UPDATE SET "
            "inputs_json = excluded.inputs_json, updated_at = excluded.updated_at",
            (email, name, inputs_json, now),
        )


def list_super_scenario_names(email):
    """Ordered names of every Super projection this email has (oldest
    first) - empty list if none yet. Mirrors list_debt_recycling_
    scenario_names() exactly."""
    if not email:
        return []
    with _conn() as conn:
        rows = conn.execute(
            "SELECT name FROM super_scenario_names WHERE email = ? ORDER BY created_at",
            (email,),
        ).fetchall()
    return [r[0] for r in rows]


def create_super_scenario(email, name):
    """Registers a new, empty named Super projection. Idempotent (INSERT
    OR IGNORE) - mirrors create_debt_recycling_scenario()."""
    if not email or not name:
        return
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO super_scenario_names (email, name, created_at) "
            "VALUES (?, ?, ?)",
            (email, name, now),
        )


def rename_super_scenario(email, old_name, new_name):
    """Renames a named Super projection - mirrors rename_debt_recycling_
    scenario() exactly (Part 46, 13 Sep 2026: the Super tool never had a
    scenario switcher wired up at all, so this sibling function never
    existed before)."""
    if not email or not old_name or not new_name or old_name == new_name:
        return
    with _conn() as conn:
        conn.execute(
            "UPDATE super_scenario_names SET name = ? WHERE email = ? AND name = ?",
            (new_name, email, old_name),
        )
        conn.execute(
            "UPDATE super_scenarios SET name = ? WHERE email = ? AND name = ?",
            (new_name, email, old_name),
        )


def delete_super_scenario(email, name):
    """Deletes a named Super projection - mirrors delete_debt_recycling_
    scenario() exactly (Part 46, 13 Sep 2026). The "never delete the
    last remaining scenario" guard lives in the shared
    _render_named_plan_switcher UI (app.py), same as every other tool
    wired to that switcher - this function has no guard of its own,
    matching its sibling."""
    if not email or not name:
        return
    with _conn() as conn:
        conn.execute(
            "DELETE FROM super_scenario_names WHERE email = ? AND name = ?",
            (email, name),
        )
        conn.execute(
            "DELETE FROM super_scenarios WHERE email = ? AND name = ?",
            (email, name),
        )


def ensure_default_super_scenario(email):
    """Guarantees at least one named Super projection exists for a
    signed-in email - mirrors ensure_default_debt_recycling_scenario().
    Called once at the top of the Super tool's render."""
    if not email:
        return
    if not list_super_scenario_names(email):
        create_super_scenario(email, _DEFAULT_SUPER_SCENARIO_NAME)


def get_super_scenario(email, name):
    """{"inputs": {...}} or None if this email has never saved this named
    Super projection. inputs is exactly the dict save_super_scenario()
    was last called with for this name - the caller (app.py) owns its
    own shape. Mirrors get_debt_recycling_scenario()."""
    if not email or not name:
        return None
    with _conn() as conn:
        row = conn.execute(
            "SELECT inputs_json FROM super_scenarios WHERE email = ? AND name = ?",
            (email, name),
        ).fetchone()
    if not row:
        return None
    try:
        inputs = json.loads(row[0]) if row[0] else {}
    except (TypeError, ValueError):
        inputs = {}
    return {"inputs": inputs}


def save_super_scenario(email, name, inputs):
    """Upserts this ONE named Super projection - same one-row-per-name
    contract as save_debt_recycling_scenario(). Also registers `name` in
    the names registry (INSERT OR IGNORE) as the same defensive belt-
    and-braces measure."""
    if not email or not name:
        return
    now = datetime.now(timezone.utc).isoformat()
    inputs_json = json.dumps(inputs or {})
    with _conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO super_scenario_names (email, name, created_at) "
            "VALUES (?, ?, ?)",
            (email, name, now),
        )
        conn.execute(
            "INSERT INTO super_scenarios (email, name, inputs_json, updated_at) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(email, name) DO UPDATE SET "
            "inputs_json = excluded.inputs_json, updated_at = excluded.updated_at",
            (email, name, inputs_json, now),
        )


def list_budget_plan_names(email):
    """Ordered names of every Budget Planner plan this email has
    (oldest first) - empty list if none yet. Mirrors portfolio_store.
    list_portfolios() (Part 30, 8 Sep 2026)."""
    if not email:
        return []
    with _conn() as conn:
        rows = conn.execute(
            "SELECT name FROM budget_plan_names WHERE email = ? ORDER BY created_at",
            (email,),
        ).fetchall()
    return [r[0] for r in rows]


def create_budget_plan(email, name):
    """Registers a new, empty named plan - mirrors portfolio_store.
    create_portfolio(). Idempotent (INSERT OR IGNORE)."""
    if not email or not name:
        return
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO budget_plan_names (email, name, created_at) VALUES (?, ?, ?)",
            (email, name, now),
        )


def rename_budget_plan(email, old_name, new_name):
    if not email or not old_name or not new_name or old_name == new_name:
        return
    with _conn() as conn:
        conn.execute(
            "UPDATE budget_plan_names SET name = ? WHERE email = ? AND name = ?",
            (new_name, email, old_name),
        )
        conn.execute(
            "UPDATE budget_plans SET name = ? WHERE email = ? AND name = ?",
            (new_name, email, old_name),
        )


def delete_budget_plan(email, name):
    if not email or not name:
        return
    with _conn() as conn:
        conn.execute("DELETE FROM budget_plan_names WHERE email = ? AND name = ?", (email, name))
        conn.execute("DELETE FROM budget_plans WHERE email = ? AND name = ?", (email, name))


def ensure_default_budget_plan(email):
    """Guarantees at least one named plan exists for a signed-in email -
    same role as portfolio_store.ensure_default_portfolio(). Called once
    at the top of the Budget Planner render so a brand-new user (never
    migrated, never created one) always has something to select."""
    if not email:
        return
    if not list_budget_plan_names(email):
        create_budget_plan(email, _DEFAULT_BUDGET_PLAN_NAME)


def get_budget_plan(email, name):
    """{"money_in": float|None, "categories": {id: float}, "country":
    str|None, "years": int|None} or None if this email has never saved
    this named plan. categories only ever contains keys the plan
    actually had a value for (never a full zero-filled dict) - an
    unfilled category on the page stays unfilled on reload too."""
    if not email or not name:
        return None
    with _conn() as conn:
        row = conn.execute(
            "SELECT money_in, categories_json, country, years "
            "FROM budget_plans WHERE email = ? AND name = ?",
            (email, name),
        ).fetchone()
    if not row:
        return None
    try:
        categories = json.loads(row[1]) if row[1] else {}
    except (TypeError, ValueError):
        categories = {}
    return {"money_in": row[0], "categories": categories, "country": row[2], "years": row[3]}


def save_budget_plan(email, name, money_in, categories, country=None, years=None):
    """Upserts this ONE named plan (Part 30 - previously the one plan
    this email was allowed at all; multiple names now live side by
    side). categories is stored as-is (only the filled keys the caller
    passes) - the caller (app.py) is responsible for not passing
    None/blank entries it doesn't want persisted. Also registers `name`
    in the names registry (INSERT OR IGNORE), same defensive reasoning
    as save_debt_recycling_scenario() - this tool auto-saves on every
    render, so an orphan row here would be far easier to hit than
    Debt Recycling's explicit-button save."""
    if not email or not name:
        return
    now = datetime.now(timezone.utc).isoformat()
    categories_json = json.dumps(categories or {})
    with _conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO budget_plan_names (email, name, created_at) VALUES (?, ?, ?)",
            (email, name, now),
        )
        conn.execute(
            "INSERT INTO budget_plans "
            "(email, name, money_in, categories_json, country, years, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(email, name) DO UPDATE SET "
            "money_in = excluded.money_in, categories_json = excluded.categories_json, "
            "country = excluded.country, years = excluded.years, updated_at = excluded.updated_at",
            (email, name, money_in, categories_json, country, years, now),
        )
