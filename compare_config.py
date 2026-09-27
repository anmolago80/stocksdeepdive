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
