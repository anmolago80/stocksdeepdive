"""
signin_wall.py

SIGNIN_WALL_LIVE gate (TASK 60, Director-directed/owner-approved, 15 Nov
2026): default OFF, meaning the site behaves exactly as it does today -
everything open, no sign-in required anywhere. The owner sets the env
var in Railway once satisfied with testing; this module never reads or
sets it anywhere but here.

INVERTED SENSE vs every other *_LIVE flag in this codebase (TOP100_
PUBLIC, COMPARE_CHIP_ENTRY_LIVE, TOP200_QUALITY_ON_SALE_LIVE, etc.):
those default OFF = old/narrow behavior, ON = new feature visible to
everyone. This one is the opposite - default OFF = fully open (today),
ON = locked down behind sign-in except for the public allowlist (home,
the S&P 500 scanner, /s/<ticker> snapshots, legal/info pages, blog,
sign-in itself). There was no earlier flag of this shape to follow;
this is deliberately a freestanding module (no streamlit, no FastAPI
import) so app.py (the Streamlit subprocess) and server.py/api_v1.py
(the FastAPI process) can both import it and agree on the same
decision without either depending on the other's framework.

Owner access is NEVER decided here - every caller combines wall_live()
with its own existing ai_gate.is_owner(...) check (and, for a
gated app.py page, paywall_engine.is_logged_in()) exactly the way every
other *_LIVE gate in this codebase already does, so the owner always
sees the real, current site for review regardless of this flag.
"""

import os

# Scanner (TASK 60, section 1): the ONE universe that stays public while
# the wall is live. Matched against the resolved DISPLAY name (e.g.
# "S&P 500"), never a slug - callers already have the display name on
# hand (scanner_engine's own universe names, or api_v1._resolve_
# universe()'s return value) so no new lookup is introduced here.
PUBLIC_SCANNER_UNIVERSE = "S&P 500"


def wall_live():
    """True once the owner has set SIGNIN_WALL_LIVE=1 in Railway. Same
    unset/""/0/off=OFF, 1=ON convention as every other *_LIVE flag."""
    return os.environ.get("SIGNIN_WALL_LIVE", "0").strip() == "1"


def scanner_universe_is_public(display_name):
    """True iff this universe's display name is the one Scanner
    universe that stays visible to a signed-out visitor while the wall
    is live (TASK 60 section 1: "Scanner: S&P 500 universe ONLY")."""
    return (display_name or "").strip() == PUBLIC_SCANNER_UNIVERSE
