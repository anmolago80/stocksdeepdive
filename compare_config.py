"""
compare_config.py

Single shared cap for "how many tickers can one Comparison request
cover" - Commit 2 (27 Sep 2026, owner-directed): the Streamlit
Comparison page (app.py) and the public GET /api/v1/compare endpoint
(api_v1.py) used to each hardcode their own limit (uncapped, and 10,
respectively). Both now import COMPARE_MAX_TICKERS from here instead of
carrying their own literal, so the two can't drift apart again.

Deliberately its own tiny module rather than one importing the other:
app.py is a Streamlit script (importing anything else from it runs that
whole page-registration module top to bottom), and api_v1.py is a
FastAPI sub-app (pulling it into the Streamlit process would drag its
CORS setup, rate-limit state and route registration along for nothing).
A bare constant with no other imports needs neither.
"""

COMPARE_MAX_TICKERS = 15


import os


def compare_chip_entry_live():
    """TASK 55 (Director-directed, 13 Nov 2026) - the switch for the
    Comparison page's compact chip entry, editable saved lists, and
    private notes column. Same unset/""/0/off=OFF, 1/on=ON pattern as
    every other *_LIVE gate in this codebase (e.g. top100_engine.
    top200_sticky_buffer_live()) - re-read on every call, never
    cached, so a changed Railway variable takes effect on the very
    next page render with no redeploy. Claude Code never sets this -
    the Director does, on Railway, only after reviewing the new
    version (app.py's own call sites OR this, same "owner always sees
    it for review" pattern as every prior *_LIVE gate)."""
    raw = (os.environ.get("COMPARE_CHIP_ENTRY_LIVE") or "").strip().lower()
    return raw in ("1", "on")
