"""
portfolio_store.py

Per-user "Invested" holdings for signed-in visitors - the entry point for
the long-term Portfolio tracker (mirrors the desktop Portfolio Health
Monitor app's Invested-tab + locked-baseline design, but multi-tenant:
every row is scoped to the signed-in user's email, so one visitor never
sees another's holdings).

MULTIPLE NAMED PORTFOLIOS: a user can split their holdings across more
than one named portfolio (e.g. "SMSF" and "Personal") - including holding
the SAME ticker in more than one portfolio at once, each with its own
shares/buy price/baseline. The `portfolios` table is the registry of a
user's portfolio names (so an empty, just-created portfolio still shows
up); every holding row is scoped by (email, portfolio, ticker), which is
also now the primary key - the same ticker can only appear once WITHIN a
given portfolio, but can appear in as many different portfolios as the
user likes, each tracked completely independently (its own shares, buy
price/date, thesis, and - critically - its own locked baseline, captured
the first time that (email, portfolio, ticker) triple is added). The one
exception is `iv_overrides` (manual intrinsic-value override): that
stays keyed by (email, ticker) with no portfolio dimension, since a
manual "what I think this company is worth" estimate is a property of
the ticker, not of which account happens to hold it - added twice over
in SMSF and Personal, it's still the same override.

WHERE THE FILE LIVES / WHY SQLITE: identical rule to every other store in
this app - the attached Railway Volume when one exists, falling back to
this directory locally (see watchlist_store.py); SQLite serialises
concurrent writers safely, a hand-rolled JSON read-modify-write does not.

IDENTITY: the signed-in email from paywall_engine.current_user_email()
(Google or email-code sign-in - both already live on this site). This
module never sees or stores anything else about who the user is, and
every read/write below takes `email` as its first argument and scopes
its query to exactly that value - there is no code path here that can
return one user's holdings for another user's email.

THE BASELINE LOCK (same philosophy as the desktop app's baselines_store.py
+ spreadsheet_engine.py): a holding's baseline - the fundamentals
snapshot it's judged against later - is captured ONCE, the first time
that (email, portfolio, ticker) triple is added, and never silently
overwritten after that (add_holding() below is INSERT OR IGNORE on that
primary key). Two baseline shapes exist, tagged by the "schema" key
inside baseline_json so a later reader always knows which it has:
  - "desktop_v1"  - carried over as-is from the desktop app's own
                    baselines.json for the four holdings imported once
                    for anmolago@hotmail.com (see seed_desktop_import()).
  - "website_v1"  - captured on the website itself via
                    nightly_scan.analyze_ticker_lite() (the SAME scoring
                    engine every other page on this site already uses -
                    deliberately NOT a separate/ported copy) for any
                    holding added going forward.

SCHEMA MIGRATION: this module originally shipped with portfolio_holdings
keyed by (email, ticker) only - one portfolio per user, no name. See
_migrate_legacy_schema() below for how any already-live rows in that
shape are folded into a named portfolio in place, without losing
anything, the first time this module runs after the upgrade.
"""

import json
import os
import sqlite3
from datetime import datetime, timezone


def _data_dir():
    return os.environ.get("RAILWAY_VOLUME_MOUNT_PATH") or os.path.dirname(__file__)


DB_PATH = os.path.join(_data_dir(), "stocksdeepdive.db")

# The one-off desktop-import owner (see seed_desktop_import at the bottom)
# is also the one whose pre-existing, pre-multi-portfolio holdings are
# known to be their super - _migrate_legacy_schema() below uses this same
# constant to fold those specific rows into a portfolio named "SMSF"
# rather than the generic "Main" every other already-live user's legacy
# rows fold into.
_SEED_OWNER_EMAIL = "anmolago@hotmail.com"
_SEED_OWNER_LEGACY_PORTFOLIO = "SMSF"
_DEFAULT_LEGACY_PORTFOLIO = "Main"


def _conn():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS portfolios (
            email TEXT NOT NULL,
            name TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (email, name)
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS portfolio_holdings (
            email TEXT NOT NULL,
            portfolio TEXT NOT NULL,
            ticker TEXT NOT NULL,
            name TEXT,
            kind TEXT NOT NULL DEFAULT 'STOCK',
            currency TEXT,
            shares REAL,
            buy_price REAL,
            buy_date TEXT,
            thesis TEXT NOT NULL DEFAULT '',
            thesis_drivers_json TEXT NOT NULL DEFAULT '[]',
            baseline_json TEXT NOT NULL DEFAULT '{}',
            baseline_date TEXT,
            source TEXT NOT NULL DEFAULT 'website',
            added_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (email, portfolio, ticker)
        )"""
    )
    # Services batch 3, Part A2: per-holding franking percentage (0-100),
    # NULL = unset. Yahoo has no franking data and there's no reliable
    # free feed, so this is a private, user-entered setting - never
    # shown anywhere public, never sent to the API/MCP/snapshots (see
    # snapshot_store._PUBLIC_FIELD_MAP, which has no entry for it).
    # Guarded ALTER TABLE, same belt-and-braces pattern this module's own
    # watchdog_enabled column (below) and positions_store.py already use.
    try:
        conn.execute("ALTER TABLE portfolio_holdings ADD COLUMN franking_pct REAL")
    except sqlite3.OperationalError:
        pass
    # One row per email that has ever received the one-off desktop-import
    # seed (see seed_desktop_import) - keeps that seed from ever re-firing
    # for that email again, even if every seeded holding is later deleted.
    conn.execute(
        """CREATE TABLE IF NOT EXISTS portfolio_seed_log (
            email TEXT PRIMARY KEY,
            seeded_at TEXT NOT NULL
        )"""
    )
    # Per-user, per-ticker manual intrinsic-value override (1d) - lets an
    # owner substitute their own number for the site's DCF model without
    # touching the model itself. One row per (email, ticker); absence means
    # "use the model value". Deliberately NOT scoped by portfolio - see
    # the module docstring's "one exception" note.
    conn.execute(
        """CREATE TABLE IF NOT EXISTS iv_overrides (
            email TEXT NOT NULL,
            ticker TEXT NOT NULL,
            value REAL NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (email, ticker)
        )"""
    )
    # Optional per-user, per-portfolio numbers (Part 2 workbook parity) the
    # site has no other way to know: total capital ever transferred into
    # THAT portfolio, and cash currently held outside any holding in it.
    # Both stay unset (row absent) until the user fills in "Portfolio
    # settings" for that portfolio - every reader of this table must treat
    # a missing row as "hide the derived figures", never as zero.
    conn.execute(
        """CREATE TABLE IF NOT EXISTS portfolio_settings (
            email TEXT NOT NULL,
            portfolio TEXT NOT NULL,
            total_transferred REAL,
            cash_held REAL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (email, portfolio)
        )"""
    )
    # AI-readiness roadmap Phase 5: per-portfolio opt-in for the nightly
    # Portfolio AI watchdog (portfolio_watchdog_engine.py) - defaults to 0
    # (off) for every portfolio, including every one that already existed
    # before this column did, so nothing starts sending AI-written email/
    # push until a user explicitly turns it on in Portfolio settings.
    # Guarded ALTER TABLE, same belt-and-braces pattern positions_store.py
    # uses for its own added-later column.
    try:
        conn.execute(
            "ALTER TABLE portfolio_settings ADD COLUMN "
            "watchdog_enabled INTEGER NOT NULL DEFAULT 0"
        )
    except sqlite3.OperationalError:
        pass
    _migrate_legacy_schema(conn)
    return conn


def _table_columns(conn, table):
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]


def _migrate_legacy_schema(conn):
    """One-time, idempotent fold-up from the original single-portfolio
    shape into the multi-portfolio one. Runs on every connection but is a
    single cheap PRAGMA check once already migrated, so it costs nothing
    after the first call post-upgrade.

    Two tables predate the `portfolio` column: portfolio_holdings (was PK
    (email, ticker)) and portfolio_settings (was PK (email,)). Both get
    rebuilt with the new PK and every existing row assigned to a named
    portfolio - "SMSF" for the seed owner (see the module docstring:
    those specific holdings are known to be their super), "Main" for
    every other already-live user - so nothing already tracked
    disappears, silently merges, or has to be re-entered.
    """
    _holdings_cols = _table_columns(conn, "portfolio_holdings")
    if "portfolio" not in _holdings_cols:
        conn.execute("ALTER TABLE portfolio_holdings RENAME TO portfolio_holdings_pre_multi")
        conn.execute(
            """CREATE TABLE portfolio_holdings (
                email TEXT NOT NULL,
                portfolio TEXT NOT NULL,
                ticker TEXT NOT NULL,
                name TEXT,
                kind TEXT NOT NULL DEFAULT 'STOCK',
                currency TEXT,
                shares REAL,
                buy_price REAL,
                buy_date TEXT,
                thesis TEXT NOT NULL DEFAULT '',
                thesis_drivers_json TEXT NOT NULL DEFAULT '[]',
                baseline_json TEXT NOT NULL DEFAULT '{}',
                baseline_date TEXT,
                source TEXT NOT NULL DEFAULT 'website',
                added_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (email, portfolio, ticker)
            )"""
        )
        _old_cols = _table_columns(conn, "portfolio_holdings_pre_multi")
        _old_rows = conn.execute(f"SELECT {', '.join(_old_cols)} FROM portfolio_holdings_pre_multi").fetchall()
        _now = datetime.now(timezone.utc).isoformat()
        for row in _old_rows:
            d = dict(zip(_old_cols, row))
            portfolio = (_SEED_OWNER_LEGACY_PORTFOLIO if d["email"] == _SEED_OWNER_EMAIL
                         else _DEFAULT_LEGACY_PORTFOLIO)
            conn.execute(
                "INSERT OR IGNORE INTO portfolios (email, name, created_at) VALUES (?, ?, ?)",
                (d["email"], portfolio, _now),
            )
            conn.execute(
                "INSERT OR IGNORE INTO portfolio_holdings "
                "(email, portfolio, ticker, name, kind, currency, shares, buy_price, "
                "buy_date, thesis, thesis_drivers_json, baseline_json, baseline_date, "
                "source, added_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (d["email"], portfolio, d["ticker"], d["name"], d["kind"], d["currency"],
                 d["shares"], d["buy_price"], d["buy_date"], d["thesis"],
                 d["thesis_drivers_json"], d["baseline_json"], d["baseline_date"],
                 d["source"], d["added_at"], d["updated_at"]),
            )
        conn.execute("DROP TABLE portfolio_holdings_pre_multi")

    _settings_cols = _table_columns(conn, "portfolio_settings")
    if _settings_cols and "portfolio" not in _settings_cols:
        conn.execute("ALTER TABLE portfolio_settings RENAME TO portfolio_settings_pre_multi")
        conn.execute(
            """CREATE TABLE portfolio_settings (
                email TEXT NOT NULL,
                portfolio TEXT NOT NULL,
                total_transferred REAL,
                cash_held REAL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (email, portfolio)
            )"""
        )
        _old_cols = _table_columns(conn, "portfolio_settings_pre_multi")
        _old_rows = conn.execute(f"SELECT {', '.join(_old_cols)} FROM portfolio_settings_pre_multi").fetchall()
        for row in _old_rows:
            d = dict(zip(_old_cols, row))
            portfolio = (_SEED_OWNER_LEGACY_PORTFOLIO if d["email"] == _SEED_OWNER_EMAIL
                         else _DEFAULT_LEGACY_PORTFOLIO)
            conn.execute(
                "INSERT OR IGNORE INTO portfolio_settings "
                "(email, portfolio, total_transferred, cash_held, updated_at) VALUES (?, ?, ?, ?, ?)",
                (d["email"], portfolio, d["total_transferred"], d["cash_held"], d["updated_at"]),
            )
        conn.execute("DROP TABLE portfolio_settings_pre_multi")


# --------------------------------------------------------------------------
# Portfolio registry - create/list/rename/delete a named portfolio. A
# portfolio can exist with zero holdings (created ahead of adding
# anything to it), which is why this is a real table and not just
# DISTINCT portfolio values off portfolio_holdings.
# --------------------------------------------------------------------------

def list_portfolios(email):
    """Every portfolio name this user has, oldest-created first. Never
    empty for a user who has been through page_portfolio() at least once
    (see ensure_default_portfolio) - empty only for an email this module
    has never seen."""
    if not email:
        return []
    with _conn() as conn:
        rows = conn.execute(
            "SELECT name FROM portfolios WHERE email = ? ORDER BY created_at ASC",
            (email,),
        ).fetchall()
    return [r[0] for r in rows]


def create_portfolio(email, name):
    """INSERT OR IGNORE on (email, name): creating a portfolio that
    already exists is a no-op. Returns True if a new portfolio was
    actually created."""
    name = (name or "").strip()
    if not email or not name:
        return False
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO portfolios (email, name, created_at) VALUES (?, ?, ?)",
            (email, name, now),
        )
        return cur.rowcount > 0


def ensure_default_portfolio(email):
    """Called on every Portfolio page load: if this user has no portfolio
    at all yet (brand new visitor, or an email seed_desktop_import never
    touches), give them one named "Main" so the portfolio selector is
    never empty and "Add a holding" always has somewhere to go. No-op for
    anyone who already has at least one portfolio."""
    if not email:
        return
    if not list_portfolios(email):
        create_portfolio(email, _DEFAULT_LEGACY_PORTFOLIO)


def rename_portfolio(email, old_name, new_name):
    """Rename a portfolio and cascade the rename onto every holding and
    settings row filed under its old name. No-op if old_name doesn't
    exist for this user, or if new_name is blank/unchanged."""
    old_name = (old_name or "").strip()
    new_name = (new_name or "").strip()
    if not email or not old_name or not new_name or old_name == new_name:
        return False
    with _conn() as conn:
        cur = conn.execute(
            "UPDATE portfolios SET name = ? WHERE email = ? AND name = ?",
            (new_name, email, old_name),
        )
        if cur.rowcount == 0:
            return False
        conn.execute(
            "UPDATE portfolio_holdings SET portfolio = ? WHERE email = ? AND portfolio = ?",
            (new_name, email, old_name),
        )
        conn.execute(
            "UPDATE portfolio_settings SET portfolio = ? WHERE email = ? AND portfolio = ?",
            (new_name, email, old_name),
        )
        return True


def delete_portfolio(email, name):
    """Delete a portfolio AND every holding/settings row filed under it.
    Irreversible - the caller (the UI) is responsible for confirming with
    the user first, same as remove_holding()."""
    name = (name or "").strip()
    if not email or not name:
        return
    with _conn() as conn:
        conn.execute("DELETE FROM portfolios WHERE email = ? AND name = ?", (email, name))
        conn.execute("DELETE FROM portfolio_holdings WHERE email = ? AND portfolio = ?", (email, name))
        conn.execute("DELETE FROM portfolio_settings WHERE email = ? AND portfolio = ?", (email, name))


def get_iv_override(email, ticker):
    if not email or not ticker:
        return None
    with _conn() as conn:
        row = conn.execute(
            "SELECT value FROM iv_overrides WHERE email = ? AND ticker = ?",
            (email, ticker.upper()),
        ).fetchone()
    return row[0] if row else None


def set_iv_override(email, ticker, value):
    if not email or not ticker or value is None:
        return
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as conn:
        conn.execute(
            "INSERT INTO iv_overrides (email, ticker, value, updated_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(email, ticker) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (email, ticker.upper(), float(value), now),
        )


def clear_iv_override(email, ticker):
    if not email or not ticker:
        return
    with _conn() as conn:
        conn.execute(
            "DELETE FROM iv_overrides WHERE email = ? AND ticker = ?",
            (email, ticker.upper()),
        )


def get_settings(email, portfolio):
    """{'total_transferred': float|None, 'cash_held': float|None} - both
    None (not 0) when the user has never filled in this portfolio's
    Portfolio settings expander, so callers can tell "not set" from "set
    to zero"."""
    if not email or not portfolio:
        return {"total_transferred": None, "cash_held": None}
    with _conn() as conn:
        row = conn.execute(
            "SELECT total_transferred, cash_held FROM portfolio_settings WHERE email = ? AND portfolio = ?",
            (email, portfolio),
        ).fetchone()
    if not row:
        return {"total_transferred": None, "cash_held": None}
    return {"total_transferred": row[0], "cash_held": row[1]}


def get_settings_all(email):
    """Every portfolio's settings for this user, summed for the "All
    portfolios" combined view: {'total_transferred': float|None,
    'cash_held': float|None}, each None only if NOT A SINGLE portfolio
    has that figure set (mirrors get_settings()'s "None means unset, 0 is
    a real value" contract) - otherwise it's the sum of whichever
    portfolios did set it, same "exclude rather than guess" rule the rest
    of this page's totals already follow for missing prices/FX."""
    if not email:
        return {"total_transferred": None, "cash_held": None}
    with _conn() as conn:
        rows = conn.execute(
            "SELECT total_transferred, cash_held FROM portfolio_settings WHERE email = ?",
            (email,),
        ).fetchall()
    _transferred = [r[0] for r in rows if r[0] is not None]
    _cash = [r[1] for r in rows if r[1] is not None]
    return {
        "total_transferred": sum(_transferred) if _transferred else None,
        "cash_held": sum(_cash) if _cash else None,
    }


def set_settings(email, portfolio, total_transferred=None, cash_held=None):
    if not email or not portfolio:
        return
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as conn:
        conn.execute(
            "INSERT INTO portfolio_settings (email, portfolio, total_transferred, cash_held, updated_at) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT(email, portfolio) DO UPDATE SET "
            "total_transferred = excluded.total_transferred, cash_held = excluded.cash_held, "
            "updated_at = excluded.updated_at",
            (email, portfolio, total_transferred, cash_held, now),
        )


def get_watchdog_enabled(email, portfolio):
    """AI-readiness roadmap Phase 5: whether this portfolio has the
    nightly AI watchdog turned on. False (never sent) for any portfolio
    with no portfolio_settings row at all, same "missing row = not set /
    off" convention get_settings() above already uses."""
    if not email or not portfolio:
        return False
    with _conn() as conn:
        row = conn.execute(
            "SELECT watchdog_enabled FROM portfolio_settings WHERE email = ? AND portfolio = ?",
            (email, portfolio),
        ).fetchone()
    return bool(row[0]) if row else False


def set_watchdog_enabled(email, portfolio, enabled):
    """Kept deliberately separate from set_settings() above (same
    separation-of-concerns precedent as update_thesis() vs
    update_position()) so toggling the watchdog can never accidentally
    clobber total_transferred/cash_held, and vice versa - each is its own
    UPSERT touching only its own column(s)."""
    if not email or not portfolio:
        return
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as conn:
        conn.execute(
            "INSERT INTO portfolio_settings (email, portfolio, watchdog_enabled, updated_at) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(email, portfolio) DO UPDATE SET "
            "watchdog_enabled = excluded.watchdog_enabled, updated_at = excluded.updated_at",
            (email, portfolio, 1 if enabled else 0, now),
        )


def list_portfolio_owners():
    """[(email, portfolio), ...] for every portfolio that has the AI
    watchdog turned on AND has at least one holding - the nightly
    watchdog's (portfolio_watchdog_engine.py) fan-out list. Mirrors
    watchlist_store.all_users()'s role for the weekly digest: the one
    function that answers "who should this background job visit tonight"
    without a live Streamlit session to ask current_user_email()."""
    with _conn() as conn:
        rows = conn.execute(
            """SELECT DISTINCT ps.email, ps.portfolio FROM portfolio_settings ps
               WHERE ps.watchdog_enabled = 1
               AND EXISTS (
                   SELECT 1 FROM portfolio_holdings ph
                   WHERE ph.email = ps.email AND ph.portfolio = ps.portfolio
               )
               ORDER BY ps.email, ps.portfolio"""
        ).fetchall()
    return [(r[0], r[1]) for r in rows]


def _row_to_dict(row):
    (email, portfolio, ticker, name, kind, currency, shares, buy_price, buy_date,
     thesis, thesis_drivers_json, baseline_json, baseline_date, source,
     added_at, updated_at, franking_pct) = row
    try:
        thesis_drivers = json.loads(thesis_drivers_json) or []
    except (TypeError, ValueError):
        thesis_drivers = []
    try:
        baseline = json.loads(baseline_json) or {}
    except (TypeError, ValueError):
        baseline = {}
    return {
        "email": email, "portfolio": portfolio, "ticker": ticker, "name": name,
        "kind": kind, "currency": currency, "shares": shares, "buy_price": buy_price,
        "buy_date": buy_date, "thesis": thesis,
        "thesis_drivers": thesis_drivers, "baseline": baseline,
        "baseline_date": baseline_date, "source": source,
        "added_at": added_at, "updated_at": updated_at,
        # Services batch 3, Part A2: None = unset (never shown as 0%).
        "franking_pct": franking_pct,
    }


_HOLDING_COLUMNS = (
    "email, portfolio, ticker, name, kind, currency, shares, buy_price, "
    "buy_date, thesis, thesis_drivers_json, baseline_json, "
    "baseline_date, source, added_at, updated_at, franking_pct"
)


def get_holdings(email, portfolio):
    """Every holding in one of this user's portfolios, oldest-added first
    (empty list if none / no email or portfolio - never falls back to
    "everyone's holdings" or "every portfolio", the one invariant this
    whole module exists to guarantee)."""
    if not email or not portfolio:
        return []
    with _conn() as conn:
        rows = conn.execute(
            f"SELECT {_HOLDING_COLUMNS} FROM portfolio_holdings "
            "WHERE email = ? AND portfolio = ? ORDER BY added_at ASC",
            (email, portfolio),
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def get_holdings_all(email):
    """Every holding across every one of this user's portfolios, for the
    "All portfolios" combined view - each row still carries its own
    "portfolio" field, so a ticker held in more than one portfolio comes
    back as more than one row rather than being merged into one (that
    merge would silently average together two different buy prices/
    baselines/theses - see the module docstring)."""
    if not email:
        return []
    with _conn() as conn:
        rows = conn.execute(
            f"SELECT {_HOLDING_COLUMNS} FROM portfolio_holdings "
            "WHERE email = ? ORDER BY portfolio ASC, added_at ASC",
            (email,),
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def all_portfolio_tickers():
    """Services batch 2, Part 4 (2026-09-01): distinct tickers held in
    ANY portfolio, across every user - not scoped to one email, unlike
    every other function in this module (see the module docstring's own
    "never falls back to everyone's holdings" invariant, which is about
    HOLDINGS DATA - price/shares/thesis/etc - not this). This is a bare
    ticker list with no user or holding detail attached, used only to
    widen the weekly earnings-calendar watch list (scheduler_engine.
    _run_earnings_refresh) so a ticker anyone holds gets its report
    dates tracked, even one nobody has ever followed or that fell out of
    every scanned universe."""
    with _conn() as conn:
        rows = conn.execute("SELECT DISTINCT ticker FROM portfolio_holdings").fetchall()
    return [r[0] for r in rows if r[0]]


def get_holding(email, portfolio, ticker):
    if not email or not portfolio or not ticker:
        return None
    with _conn() as conn:
        row = conn.execute(
            f"SELECT {_HOLDING_COLUMNS} FROM portfolio_holdings "
            "WHERE email = ? AND portfolio = ? AND ticker = ?",
            (email, portfolio, ticker.upper()),
        ).fetchone()
    return _row_to_dict(row) if row else None


def has_holding(email, portfolio, ticker):
    return get_holding(email, portfolio, ticker) is not None


def add_holding(email, portfolio, ticker, name="", kind="STOCK", currency="AUD",
                shares=0.0, buy_price=0.0, buy_date=None, thesis="",
                thesis_drivers=None, baseline=None, baseline_date=None,
                source="website"):
    """
    Add one holding to `portfolio` for `email` and lock its baseline.
    INSERT OR IGNORE on (email, portfolio, ticker): if this user already
    has this ticker IN THIS PORTFOLIO, the existing row (and its
    already-locked baseline) is left completely untouched - re-adding a
    ticker to the same portfolio is a no-op, never a silent overwrite of
    the lock. The same ticker can still be added to a DIFFERENT portfolio
    for this user with no conflict - that's a separate row entirely.
    Also registers `portfolio` in the portfolios table if it doesn't
    already exist, so adding a holding to a not-yet-created portfolio
    name just creates it. Returns True if a new row was actually
    inserted, False if it already existed.
    """
    if not email or not portfolio or not ticker:
        return False
    create_portfolio(email, portfolio)
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO portfolio_holdings "
            "(email, portfolio, ticker, name, kind, currency, shares, buy_price, "
            "buy_date, thesis, thesis_drivers_json, baseline_json, "
            "baseline_date, source, added_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (email, portfolio, ticker.upper(), name, kind, currency, shares, buy_price,
             buy_date, thesis, json.dumps(thesis_drivers or []),
             json.dumps(baseline or {}), baseline_date, source, now, now),
        )
        return cur.rowcount > 0


def update_thesis(email, portfolio, ticker, thesis, thesis_drivers=None):
    """Revise a holding's thesis text/drivers WITHOUT touching its locked
    baseline - the same separation the desktop app's baselines_store
    (fundamentals) vs theses.json (thesis) keeps, so refining a thesis
    later never re-captures or perturbs the baseline snapshot."""
    if not email or not portfolio or not ticker:
        return
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as conn:
        conn.execute(
            "UPDATE portfolio_holdings SET thesis = ?, thesis_drivers_json = ?, "
            "updated_at = ? WHERE email = ? AND portfolio = ? AND ticker = ?",
            (thesis, json.dumps(thesis_drivers or []), now, email, portfolio, ticker.upper()),
        )


def update_position(email, portfolio, ticker, shares=None, buy_price=None, buy_date=None):
    """Correct a holding's cost-basis fields (shares / buy price / buy
    date) - deliberately separate from the locked baseline snapshot
    (baseline_json / baseline_date), which this never touches, same
    separation-of-concerns as update_thesis() above. Only the fields
    passed (not None) are changed."""
    if not email or not portfolio or not ticker:
        return
    existing = get_holding(email, portfolio, ticker)
    if not existing:
        return
    shares = existing["shares"] if shares is None else shares
    buy_price = existing["buy_price"] if buy_price is None else buy_price
    buy_date = existing["buy_date"] if buy_date is None else buy_date
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as conn:
        conn.execute(
            "UPDATE portfolio_holdings SET shares = ?, buy_price = ?, buy_date = ?, "
            "updated_at = ? WHERE email = ? AND portfolio = ? AND ticker = ?",
            (shares, buy_price, buy_date, now, email, portfolio, ticker.upper()),
        )


def update_franking_pct(email, portfolio, ticker, franking_pct):
    """Set (or clear, with None) one holding's franking percentage
    (Services batch 3, Part A2) - private, never touches the locked
    baseline, same separation-of-concerns as update_thesis()/
    update_position() above. `franking_pct` is stored exactly as given
    (0-100, or None to mark it unset again) - no clamping here, the
    holding-editor UI's own st.number_input(min_value=0, max_value=100)
    is what actually constrains user entry."""
    if not email or not portfolio or not ticker:
        return
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as conn:
        conn.execute(
            "UPDATE portfolio_holdings SET franking_pct = ?, updated_at = ? "
            "WHERE email = ? AND portfolio = ? AND ticker = ?",
            (franking_pct, now, email, portfolio, ticker.upper()),
        )


def set_franking_pct_for_all_au(email, portfolio, franking_pct):
    """Bulk-set franking_pct on every .AX holding in `portfolio` at once
    (Services batch 3, Part A2's "Set franking % for all AU holdings"
    convenience line in Portfolio settings) - never touches a non-.AX
    holding, franking being an AU-imputation-only concept. Returns the
    number of holdings updated."""
    if not email or not portfolio:
        return 0
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as conn:
        cur = conn.execute(
            "UPDATE portfolio_holdings SET franking_pct = ?, updated_at = ? "
            "WHERE email = ? AND portfolio = ? AND ticker LIKE '%.AX'",
            (franking_pct, now, email, portfolio),
        )
        return cur.rowcount


def remove_holding(email, portfolio, ticker):
    if not email or not portfolio or not ticker:
        return
    with _conn() as conn:
        conn.execute(
            "DELETE FROM portfolio_holdings WHERE email = ? AND portfolio = ? AND ticker = ?",
            (email, portfolio, ticker.upper()),
        )


# --------------------------------------------------------------------------
# ONE-OFF DESKTOP IMPORT - anmolago@hotmail.com's existing holdings, ported
# once from the desktop Portfolio Health Monitor app so the website starts
# from the SAME locked baselines instead of re-capturing "today" as if
# these were bought this instant. Values below are transcribed exactly
# from that app's own data on 2026-08-27:
#   - ticker / shares / buy_price / buy_date: the SMSF workbook's Invested
#     tab ("Purchased Price (Base Currency)" column - the stock's own
#     listing currency, not the AUD-converted column).
#   - name / kind / currency / baseline / baseline_date: that app's
#     data/baselines.json (the locked, first-seen fundamentals snapshot -
#     carried over UNCHANGED, never re-derived here).
#   - thesis / thesis_drivers: that app's data/theses.json (only CSL.AX
#     had one recorded).
# Nothing else from that app (its other 4 historical tickers with no
# current Invested-tab row, its scores/news caches) is imported - this is
# a starting point for the holdings actually owned today, not a full
# archive migration. All four land in the "SMSF" portfolio - literally
# the SMSF workbook these were transcribed from.
# --------------------------------------------------------------------------

_CSL_AX_THESIS = (
    "CSL is a high-quality, large-cap healthcare compounder built on a "
    "plasma-therapies franchise (CSL Behring, ~72% of revenue - "
    "immunoglobulins, albumin, clotting factors, gene therapies), plus "
    "influenza vaccines (Seqirus, ~14%) and iron/nephrology (Vifor, "
    "~14%). Its moat is structural: control of the plasma "
    "collection-and-fractionation supply chain (highest concentrated "
    "capacity, superior yield and cost per litre), high regulatory "
    "barriers, and deep behavioural lock-in from essential, "
    "doctor-prescribed products in an oligopoly (vs Grifols and Takeda). "
    "The core investment case is that the market has over-penalised CSL "
    "for the Vifor capital-allocation error, margin compression and a "
    "CEO transition, leaving it at a discounted ~11-13x P/E versus a "
    "15-18x base case - a behavioural mispricing (deprival, "
    "social-proof and authority tendencies). Underlying demand is "
    "structural (~3-5% patient growth), segment margins remain high "
    "(~40-50%), earnings growth ~9-10%, and management is taking the "
    "right corrective steps (restructuring, plasma cost/yield programs, "
    "a leadership/board reset and an active A$750m buyback). The key "
    "risk is ROIC settling structurally lower (~11-14% vs a historical "
    "~20%) rather than a permanent impairment, alongside donor-cost "
    "pressure, regulatory tightening and Vifor underperformance - risks "
    "judged high-probability but cyclical and mean-reverting (~69% "
    "full-recovery probability vs ~31% slower). Net: a resilient, "
    "cash-generative franchise bought at a discount, where the downside "
    "skews to bond-like returns and delayed recovery rather than "
    "capital loss."
)
_CSL_AX_THESIS_DRIVERS = [
    "plasma", "immunoglobulin", "IG", "albumin", "clotting factor",
    "CSL Behring", "Behring", "Seqirus", "vaccine", "influenza",
    "flu vaccine", "Vifor", "iron deficiency", "nephrology",
    "gene therapy", "gross margin", "margin", "cost per litre",
    "plasma yield", "donor", "collection", "ROIC", "ROE", "guidance",
    "FDA approval", "R&D", "buyback", "restructuring", "CEO",
    "management", "regulatory", "regulation", "Grifols", "Takeda",
    "impairment", "earnings",
]

_SEED_HOLDINGS = [
    {
        "ticker": "GOLD.AX", "name": "Global X Physical Gold Structured",
        "kind": "STOCK", "currency": "AUD", "shares": 199,
        "buy_price": 55.2505, "buy_date": "2026-07-06",
        "baseline_date": "2026-07-26",
        "baseline": {
            "schema": "desktop_v1", "price": 55.25, "intrinsic_value": None,
            "quality_score": 50, "revenue_growth": None,
            "earnings_growth": None, "profit_margin": None, "roe": None,
            "debt_to_equity": None, "fcf": None, "fcf_growth": None,
            "dividend_rate": None,
        },
    },
    {
        "ticker": "CSL.AX", "name": "CSL Limited", "kind": "STOCK",
        "currency": "AUD", "shares": 133, "buy_price": 127.4838,
        "buy_date": "2026-07-07", "baseline_date": "2026-07-16",
        "thesis": _CSL_AX_THESIS, "thesis_drivers": _CSL_AX_THESIS_DRIVERS,
        "baseline": {
            "schema": "desktop_v1", "price": 120.0, "intrinsic_value": 196.61,
            "quality_score": 51, "revenue_growth": -0.018,
            "earnings_growth": -0.801, "profit_margin": 0.0906,
            "roe": 0.0666, "debt_to_equity": 54.438, "fcf": 1848125056,
            "fcf_growth": 0.2, "dividend_rate": 2.92,
        },
    },
    {
        "ticker": "QRE.AX",
        "name": "BetaShares Australian Resources Sector ETF", "kind": "ETF",
        "currency": "AUD", "shares": 842, "buy_price": 10.064,
        "buy_date": "2026-07-06", "baseline_date": "2026-07-26",
        "baseline": {
            "schema": "desktop_v1", "price": 9.87, "intrinsic_value": None,
            "quality_score": 50, "revenue_growth": None,
            "earnings_growth": None, "profit_margin": None, "roe": None,
            "debt_to_equity": None, "fcf": None, "fcf_growth": None,
            "dividend_rate": None,
        },
    },
    {
        "ticker": "IVV.AX", "name": "iShares S&P 500 ETF", "kind": "ETF",
        "currency": "AUD", "shares": 450, "buy_price": 72.9274,
        "buy_date": "2026-08-16", "baseline_date": "2026-08-16",
        "baseline": {
            "schema": "desktop_v1", "price": 72.9274, "intrinsic_value": None,
            "quality_score": 50, "revenue_growth": None,
            "earnings_growth": None, "profit_margin": None, "roe": None,
            "debt_to_equity": None, "fcf": None, "fcf_growth": None,
            "dividend_rate": 5.701,
        },
    },
]


def seed_desktop_import(email):
    """
    Fires AT MOST ONCE per email, and only ever for _SEED_OWNER_EMAIL - not
    a general "first-run" seed for any signed-in visitor. Safe to call on
    every page load: portfolio_seed_log makes it a no-op after the first
    successful call, and add_holding()'s own INSERT OR IGNORE means even a
    concurrent double-fire can't duplicate a row. Deleting a seeded holding
    afterward is respected forever - this never re-adds it, because the
    log entry (not "holding count == 0") is what gates it.
    """
    if email != _SEED_OWNER_EMAIL:
        return
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO portfolio_seed_log (email, seeded_at) VALUES (?, ?)",
            (email, now),
        )
        if cur.rowcount == 0:
            return  # already seeded (or raced with another process that just did)
    for h in _SEED_HOLDINGS:
        add_holding(
            email, _SEED_OWNER_LEGACY_PORTFOLIO, h["ticker"], name=h.get("name", ""),
            kind=h.get("kind", "STOCK"), currency=h.get("currency", "AUD"),
            shares=h.get("shares", 0), buy_price=h.get("buy_price", 0),
            buy_date=h.get("buy_date"), thesis=h.get("thesis", ""),
            thesis_drivers=h.get("thesis_drivers"), baseline=h.get("baseline"),
            baseline_date=h.get("baseline_date"), source="desktop_import",
        )
