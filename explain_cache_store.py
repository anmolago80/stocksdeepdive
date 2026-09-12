"""
explain_cache_store.py

AI-readiness roadmap Phase 9 ("Explain this number"): a tiny cache keyed
by (ticker, metric_key) so the underlying Anthropic call for a given
gauge/metric on a given ticker only ever runs once per
_default TTL, no matter how many visitors open that same explanation -
"cached per ticker+metric to keep cost near zero" is the roadmap's own
wording for this feature, and this module is that cache. Shared across
EVERY visitor (not per-user): the whole point is that the first
signed-in visitor who generates CSL.AX's Quality explanation pays for it
once, and every later visitor - signed in or not - reads the same
already-computed answer for free (see app.py's _render_explain_popover,
which shows a fresh cache hit to anyone, but only lets a signed-in
visitor trigger a fresh generation).

Same SQLite file / volume-resolution rule as every other small store in
this app (see watchlist_store.py's module docstring) - concurrent
Streamlit sessions read/write this at once, so SQLite over a hand-rolled
JSON file.

Nothing here is private: an explanation is a description of a PUBLIC,
already-on-the-page number (a Quality Score, a Moat Score, ...), grounded
only in Methodology + that ticker's own already-public Deep Dive figures
- never a user's own portfolio data (Phase 5/7/8's private-data features
are gated for a different reason: the DATA itself is private; nothing
here ever is).
"""

import os
import sqlite3
from datetime import datetime, timedelta, timezone


def _data_dir():
    return os.environ.get("RAILWAY_VOLUME_MOUNT_PATH") or os.path.dirname(__file__)


DB_PATH = os.path.join(_data_dir(), "stocksdeepdive.db")


def _conn():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS explain_cache (
            ticker TEXT NOT NULL,
            metric_key TEXT NOT NULL,
            explanation TEXT NOT NULL,
            model TEXT,
            created_at TEXT NOT NULL,
            PRIMARY KEY (ticker, metric_key)
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS explain_cache_meta (
            key TEXT PRIMARY KEY,
            value TEXT
        )"""
    )
    # Part 43 (12 Sep 2026, scoring-recipe protection): app.py's
    # _explain_context_text and _EXPLAIN_SYSTEM_PROMPT changed what the
    # AI is fed and told - qualitative driver tiers instead of raw
    # per-term point contributions, and an explicit instruction never to
    # state a weight/base/point value. Every explanation cached BEFORE
    # this change may still contain the old leaked numeric breakdown in
    # its own already-generated prose, and the 24h TTL alone isn't a
    # reliable purge (get_fresh() only stops SERVING a stale row - a row
    # written minutes before this deploy would still read as "fresh" for
    # up to another 24h). One-time, idempotent, guarded by this meta
    # marker so it only ever runs the first time this code executes
    # against a given database - same "one-time migration on a Railway
    # Volume" convention app.py's _load_compounder_data already uses.
    _purged = conn.execute(
        "SELECT value FROM explain_cache_meta WHERE key = 'part43_purged'"
    ).fetchone()
    if not _purged:
        conn.execute("DELETE FROM explain_cache")
        conn.execute(
            "INSERT INTO explain_cache_meta (key, value) VALUES ('part43_purged', ?)",
            (datetime.now(timezone.utc).isoformat(),),
        )
        conn.commit()

    # Amendment to Part 43 (13 Sep 2026, owner decision - numbers restored
    # on the drivers charts): app.py's _explain_context_text and
    # _EXPLAIN_SYSTEM_PROMPT changed AGAIN - back to numeric per-term
    # contributions for value_score/quality/psychology/discovery/moat, and
    # the system prompt's "never state a weight/base/point value"
    # paragraph removed. Every explanation cached under the part43_purged
    # regime was written to be deliberately qualitative-only (no numbers),
    # which would now read as inconsistent with the chart's own numbers
    # sitting right next to it - a second one-time, idempotent purge under
    # its own new marker, same convention as above (never re-runs the
    # first purge, never re-runs itself once done).
    _purged_amendment = conn.execute(
        "SELECT value FROM explain_cache_meta WHERE key = 'part43_amendment_numbers_purged'"
    ).fetchone()
    if not _purged_amendment:
        conn.execute("DELETE FROM explain_cache")
        conn.execute(
            "INSERT INTO explain_cache_meta (key, value) VALUES ('part43_amendment_numbers_purged', ?)",
            (datetime.now(timezone.utc).isoformat(),),
        )
        conn.commit()
    return conn


def get_fresh(ticker, metric_key, ttl_hours=24):
    """The cached explanation for (ticker, metric_key), or None if there
    isn't one yet or the cached one is older than ttl_hours (the same 24h
    cadence auto_compounder_engine's own Compounder View cache already
    uses on this page - see page_deep_dive's own comment on that cache).
    A stale row is left in place, not deleted - the next successful
    generation overwrites it via set() below; get_fresh() simply stops
    returning it once it's past the TTL."""
    if not ticker or not metric_key:
        return None
    with _conn() as conn:
        row = conn.execute(
            "SELECT explanation, created_at FROM explain_cache "
            "WHERE ticker = ? AND metric_key = ?",
            (ticker.strip().upper(), metric_key),
        ).fetchone()
    if not row:
        return None
    explanation, created_at = row
    try:
        created = datetime.fromisoformat(created_at)
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
    except Exception:
        return None
    if datetime.now(timezone.utc) - created > timedelta(hours=ttl_hours):
        return None
    return explanation


def set(ticker, metric_key, explanation, model=None):
    """Upsert - a fresh generation always overwrites whatever was cached
    before for this (ticker, metric_key), resetting the TTL clock."""
    if not ticker or not metric_key or not explanation:
        return
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as conn:
        conn.execute(
            """INSERT INTO explain_cache (ticker, metric_key, explanation, model, created_at)
                 VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(ticker, metric_key) DO UPDATE SET
                 explanation = excluded.explanation,
                 model = excluded.model,
                 created_at = excluded.created_at""",
            (ticker.strip().upper(), metric_key, explanation, model, now),
        )
