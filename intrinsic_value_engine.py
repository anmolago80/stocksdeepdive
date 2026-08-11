"""
Manual intrinsic-value overrides.

DELIBERATELY EMPTY. Per the scanner spec, NO cell in the output tables may be
hand-populated with a fixed number - every intrinsic value must be *computed*
from that stock's own sourced data (historical free cash flow -> DCF, or the
P/E blend fallback), or shown as N/A when the data isn't there.

This module is kept only so the resolver's import and lookup still work; the
dictionary is empty, so the resolver always falls through to the calculated
DCF / P/E-blend path. If you want to force a value for one stock, use the
per-stock "Manual FCF override" control in the app instead - that still runs
the model, it just feeds it your cash-flow number.
"""

INTRINSIC_VALUES = {}


def get_intrinsic_value(ticker):
    return INTRINSIC_VALUES.get(ticker, 0)
