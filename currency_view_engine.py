"""
currency_view_engine.py

SECTION B of instruction_top200_amendments_and_currency_view.md (5 Oct
2026, Director-directed): the home-currency setting and the currency-risk
note beside the margin of safety. Pure Python (no Streamlit dependency),
same "*_engine.py owns the logic" split as top100_engine.py/currency_risk_
engine.py.

COMMIT B1: just the switch, read here so every caller (the account-bar
widget in app.py, the Deep Dive note in COMMIT B2, the Currency Risk tool
additions in COMMIT B3, the Portfolio table in COMMIT B4) shares one
definition. COMMIT B2 adds the shared MOS-view method itself.

Display only - nothing in this module (or anything it gates) ever
changes intrinsic value, margin of safety, any score, any scan row, any
stored value, or the Top 200 selection/ranking.
"""
import os


def is_currency_view_live():
    """The switch - same unset/""/0/off=OFF, 1/on=ON pattern as
    top100_engine.is_backfill_live()/financials_classifier.
    is_financials_store_live(). Re-read on every call (cheap - one env
    var lookup) so Andrew's own go takes effect on the next request
    without a redeploy. Claude Code never sets this - the Director does,
    on Railway, after Andrew has checked the owner-only preview."""
    raw = (os.environ.get("CURRENCY_VIEW_LIVE") or "").strip().lower()
    return raw in ("1", "on")


def visible_to(email, is_owner_fn):
    """Whether the home-currency control / the Deep Dive note / the
    Currency Risk tool additions / the Portfolio table should render for
    this email right now: everyone once the switch is live, owner-only
    while it's off. `is_owner_fn` is passed in (rather than importing
    ai_gate here) so this module stays a plain, dependency-light *_engine
    module - callers pass ai_gate.is_owner."""
    return is_currency_view_live() or bool(is_owner_fn(email))
