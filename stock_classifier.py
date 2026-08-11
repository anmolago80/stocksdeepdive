"""
Manual stock-type classifications.

DELIBERATELY EMPTY. Per the scanner spec, no cell may be hand-populated - the
stock type is always derived from the stock's own sector data
(auto_stock_type_engine), and reclassified to TURNAROUND automatically when a
name trades near its 52-week low. When the sector is unknown the auto engine
returns GENERAL (a default) and the app renders it in red.

Kept only so the resolver's import and lookup still resolve; the dict is empty
so the resolver always falls through to the calculated path.
"""

CLASSIFICATIONS = {}


def get_stock_type(ticker):
    return CLASSIFICATIONS.get(ticker, "UNCLASSIFIED")
