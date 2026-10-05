"""
account_currency_store.py

SECTION B, COMMIT B1 of instruction_top200_amendments_and_currency_view.md
(5 Oct 2026, Director-directed): one saved "home currency" per signed-in
account - the currency the visitor invests from, used by the currency-risk
note beside the margin of safety (currency_view_engine.py) and, later, by
Portfolio's currency-exposure table. STEP B0 found no existing per-account
preference store anywhere in this app (the EN/ES language choice is a
browser cookie/session value, not an account setting - see app.py's
_init_lang()) and no existing "base currency" setting in Portfolio
(portfolio_health_engine.py hardcodes AUD throughout) - so this is a new,
small store, following the same per-email-row pattern portfolio_store.py's
own portfolio_settings table already uses.

Allowed values are exactly currency_risk_engine.CURRENCIES - never a
separately invented list (the task's own instruction). No default: an
account that has never set one reads back None, never a guessed currency.

Same SQLite file / volume-resolution rule as every other store in this
app (see portfolio_store.py's own docstring for why SQLite over a
hand-rolled JSON file - concurrent readers/writers).
"""
import os
import sqlite3
from datetime import datetime, timezone

import currency_risk_engine as cre


def _data_dir():
    return os.environ.get("RAILWAY_VOLUME_MOUNT_PATH") or os.path.dirname(__file__)


DB_PATH = os.path.join(_data_dir(), "stocksdeepdive.db")


def _conn():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS account_currency (
            email TEXT NOT NULL PRIMARY KEY,
            home_currency TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )"""
    )
    return conn


def get_home_currency(email):
    """None if unset or email is falsy - never a guessed default."""
    if not email:
        return None
    with _conn() as conn:
        row = conn.execute(
            "SELECT home_currency FROM account_currency WHERE email = ?", (email,)
        ).fetchone()
    return row[0] if row else None


def set_home_currency(email, currency):
    """Raises ValueError for a falsy email or a currency outside
    currency_risk_engine.CURRENCIES - the task's own "do not invent a
    list" rule, enforced here so every caller (the picker widget, any
    future admin/API path) gets the same guard for free rather than
    each re-checking CURRENCIES itself."""
    if not email:
        raise ValueError("email is required")
    currency = (currency or "").strip().upper()
    if currency not in cre.CURRENCIES:
        raise ValueError(f"unsupported currency: {currency!r}")
    with _conn() as conn:
        conn.execute(
            """INSERT INTO account_currency (email, home_currency, updated_at)
               VALUES (?, ?, ?)
               ON CONFLICT(email) DO UPDATE SET
                 home_currency = excluded.home_currency,
                 updated_at = excluded.updated_at""",
            (email, currency, datetime.now(timezone.utc).isoformat()),
        )
    return currency
