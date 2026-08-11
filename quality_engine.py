"""
Manual quality-score overrides.

DELIBERATELY EMPTY. Per the scanner spec, no cell may be hand-populated - the
quality score is always computed from the stock's own fundamentals
(auto_quality_engine). When the fundamentals aren't available the auto engine
returns a default average and the app renders it in red so it's obvious the
number is an assumption rather than sourced data.

Kept only so the resolver's import and lookup still resolve; the dict is empty
so the resolver always falls through to the calculated path.
"""

QUALITY_SCORES = {}


def get_quality_score(ticker):
    return QUALITY_SCORES.get(ticker, 50)
