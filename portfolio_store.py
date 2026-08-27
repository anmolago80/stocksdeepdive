"""
portfolio_store.py

Per-user "Invested" holdings for signed-in visitors - the entry point for
the long-term Portfolio tracker (mirrors the desktop Portfolio Health
Monitor app's Invested-tab + locked-baseline design, but multi-tenant:
every row is scoped to the signed-in user's email, so one visitor never
sees another's holdings).

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
that (email, ticker) pair is added, and never silently overwritten after
that (add_holding() below is INSERT OR IGNORE on the (email, ticker)
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
"""

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
        """CREATE TABLE IF NOT EXISTS portfolio_holdings (
            email TEXT NOT NULL,
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
            PRIMARY KEY (email, ticker)
        )"""
    )
    # One row per email that has ever received the one-off desktop-import
    # seed (see seed_desktop_import) - keeps that seed from ever re-firing
    # for that email again, even if every seeded holding is later deleted.
    conn.execute(
        """CREATE TABLE IF NOT EXISTS portfolio_seed_log (
            email TEXT PRIMARY KEY,
            seeded_at TEXT NOT NULL
        )"""
    )
    return conn


def _row_to_dict(row):
    (email, ticker, name, kind, currency, shares, buy_price, buy_date,
     thesis, thesis_drivers_json, baseline_json, baseline_date, source,
     added_at, updated_at) = row
    try:
        thesis_drivers = json.loads(thesis_drivers_json) or []
    except (TypeError, ValueError):
        thesis_drivers = []
    try:
        baseline = json.loads(baseline_json) or {}
    except (TypeError, ValueError):
        baseline = {}
    return {
        "email": email, "ticker": ticker, "name": name, "kind": kind,
        "currency": currency, "shares": shares, "buy_price": buy_price,
        "buy_date": buy_date, "thesis": thesis,
        "thesis_drivers": thesis_drivers, "baseline": baseline,
        "baseline_date": baseline_date, "source": source,
        "added_at": added_at, "updated_at": updated_at,
    }


def get_holdings(email):
    """Every holding for one signed-in user, oldest-added first (empty
    list if none / no email - never falls back to "everyone's holdings",
    the one invariant this whole module exists to guarantee)."""
    if not email:
        return []
    with _conn() as conn:
        rows = conn.execute(
            "SELECT email, ticker, name, kind, currency, shares, buy_price, "
            "buy_date, thesis, thesis_drivers_json, baseline_json, "
            "baseline_date, source, added_at, updated_at "
            "FROM portfolio_holdings WHERE email = ? ORDER BY added_at ASC",
            (email,),
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def get_holding(email, ticker):
    if not email or not ticker:
        return None
    with _conn() as conn:
        row = conn.execute(
            "SELECT email, ticker, name, kind, currency, shares, buy_price, "
            "buy_date, thesis, thesis_drivers_json, baseline_json, "
            "baseline_date, source, added_at, updated_at "
            "FROM portfolio_holdings WHERE email = ? AND ticker = ?",
            (email, ticker.upper()),
        ).fetchone()
    return _row_to_dict(row) if row else None


def has_holding(email, ticker):
    return get_holding(email, ticker) is not None


def add_holding(email, ticker, name="", kind="STOCK", currency="AUD",
                shares=0.0, buy_price=0.0, buy_date=None, thesis="",
                thesis_drivers=None, baseline=None, baseline_date=None,
                source="website"):
    """
    Add one holding for `email` and lock its baseline. INSERT OR IGNORE on
    (email, ticker): if this user already has this ticker, the existing
    row (and its already-locked baseline) is left completely untouched -
    re-adding a ticker is a no-op, never a silent overwrite of the lock.
    Returns True if a new row was actually inserted, False if it already
    existed.
    """
    if not email or not ticker:
        return False
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO portfolio_holdings "
            "(email, ticker, name, kind, currency, shares, buy_price, "
            "buy_date, thesis, thesis_drivers_json, baseline_json, "
            "baseline_date, source, added_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (email, ticker.upper(), name, kind, currency, shares, buy_price,
             buy_date, thesis, json.dumps(thesis_drivers or []),
             json.dumps(baseline or {}), baseline_date, source, now, now),
        )
        return cur.rowcount > 0


def update_thesis(email, ticker, thesis, thesis_drivers=None):
    """Revise a holding's thesis text/drivers WITHOUT touching its locked
    baseline - the same separation the desktop app's baselines_store
    (fundamentals) vs theses.json (thesis) keeps, so refining a thesis
    later never re-captures or perturbs the baseline snapshot."""
    if not email or not ticker:
        return
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as conn:
        conn.execute(
            "UPDATE portfolio_holdings SET thesis = ?, thesis_drivers_json = ?, "
            "updated_at = ? WHERE email = ? AND ticker = ?",
            (thesis, json.dumps(thesis_drivers or []), now, email, ticker.upper()),
        )


def remove_holding(email, ticker):
    if not email or not ticker:
        return
    with _conn() as conn:
        conn.execute(
            "DELETE FROM portfolio_holdings WHERE email = ? AND ticker = ?",
            (email, ticker.upper()),
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
# archive migration.
# --------------------------------------------------------------------------

_SEED_OWNER_EMAIL = "anmolago@hotmail.com"

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
            email, h["ticker"], name=h.get("name", ""), kind=h.get("kind", "STOCK"),
            currency=h.get("currency", "AUD"), shares=h.get("shares", 0),
            buy_price=h.get("buy_price", 0), buy_date=h.get("buy_date"),
            thesis=h.get("thesis", ""), thesis_drivers=h.get("thesis_drivers"),
            baseline=h.get("baseline"), baseline_date=h.get("baseline_date"),
            source="desktop_import",
        )
