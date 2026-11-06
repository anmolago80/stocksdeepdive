"""
support_levels_engine.py

TASK 36 (Director-directed, owner-reported, 6 Nov 2026): a port of the
owner's own TradingView script ("AML Long-Term Entry Framework v10.1")
LEVELS ENGINE ONLY - confirmed weekly pivot lows (ta.pivotlow(3,3)) and
the "broken after two consecutive weekly closes below" rule. Nothing
else from that script (no scores, no paths, no allocation, no Watch)
is ported - the Director's own explicit instruction.

PIVOT LOW DEFINITION (TradingView's ta.pivotlow(source, leftbars,
rightbars) semantics): a bar at index i is a confirmed pivot low when
BOTH leftbars bars before it AND rightbars bars after it exist, and
its own low is less than or equal to every low in that
leftbars+1+rightbars-wide window. A pivot can only be CONFIRMED once
rightbars additional bars have closed after it - this is a real,
unavoidable lag (the same lag the owner's own TradingView labels show:
a pivot low at week W is never known as such until week W+3, with
leftbars=rightbars=3). Ties (two adjacent bars with the identical
lowest low in a window) each independently qualify - deduping
identical-or-near-identical price levels is left to the caller, same
as the raw TradingView indicator would also plot both.

BROKEN RULE: a confirmed pivot low's price level is broken the first
time TWO CONSECUTIVE weekly closes (anywhere from that pivot's own
week onward) both close below it - a single dip-and-recover week does
NOT break it (the Director's own explicit test case). Checked forward
through every bar strictly after the pivot bar itself (the pivot bar's
own close is never compared against its own low - a pivot's defining
property is its LOW, not its close, and the very next bar is the
earliest point a "close below" can even occur).

SELECTION: among UNBROKEN pivot lows priced at or below the latest
close, the nearest one (the highest surviving price <= latest close)
is "support"; the next-highest surviving price below that is "next
support". If none survive at or below the latest close, there is no
support level at all - `lowest_since` instead reports the most recent
week BEFORE the last 8 weeks whose own close was <= today's close (the
Director's own explicit fallback rule) - a "price has not been this
low since <week>" read when no real pivot-low floor survives under it.

Every public function here is pure (no I/O, no yfinance, no pandas
required) - takes/returns plain lists/dicts of (date, price) pairs, so
it can be unit-tested directly against hand-built fixtures and reused
by whatever caller builds the actual weekly bars (resampling daily,
pence-repaired-for-.L price history to weekly, which happens upstream
of this module - see this task's own caller for that step).
"""

PIVOT_LEFT_BARS = 3
PIVOT_RIGHT_BARS = 3
WEEKS_PER_YEAR = 52
LOWEST_SINCE_EXCLUDE_RECENT_WEEKS = 8


def confirmed_pivot_lows(weekly_lows, left=PIVOT_LEFT_BARS, right=PIVOT_RIGHT_BARS):
    """weekly_lows: a list of (date, low) pairs, oldest-first (chrono-
    logical ascending) - the SAME order every function in this module
    expects throughout. Returns [(index, date, low), ...] for every
    bar that is a confirmed pivot low (see this module's own docstring
    for the exact definition) - `index` is this bar's position in
    `weekly_lows`, kept so callers (e.g. the break-rule check below)
    can relate a pivot back to its own week without a second date
    lookup. Only bars with `right` bars after them can be confirmed at
    all - too-short a series (fewer than left+right+1 bars) returns an
    empty list, never raises."""
    n = len(weekly_lows)
    out = []
    if n < left + right + 1:
        return out
    for i in range(left, n - right):
        window = weekly_lows[i - left: i + right + 1]
        this_low = weekly_lows[i][1]
        if all(this_low <= w[1] for w in window):
            out.append((i, weekly_lows[i][0], this_low))
    return out


def unbroken_pivot_lows(weekly_closes, pivots):
    """Filters `pivots` (confirmed_pivot_lows()'s own output) down to
    those NOT broken by the two-consecutive-weekly-close rule (see
    this module's own docstring). `weekly_closes` is a list of (date,
    close) pairs, the SAME oldest-first order and the SAME length/
    alignment as the `weekly_lows` list `pivots` was computed from -
    `pivots`' own stored index is used to look up each pivot's own
    position directly, never re-matched by date. Checks every bar
    STRICTLY AFTER the pivot's own bar (the pivot's own week's close
    is never compared against its own low)."""
    out = []
    for idx, date, price in pivots:
        broken = False
        consecutive_below = 0
        for j in range(idx + 1, len(weekly_closes)):
            if weekly_closes[j][1] < price:
                consecutive_below += 1
                if consecutive_below >= 2:
                    broken = True
                    break
            else:
                consecutive_below = 0
        if not broken:
            out.append((idx, date, price))
    return out


def _lowest_since(weekly_closes, latest_close,
                   exclude_recent_weeks=LOWEST_SINCE_EXCLUDE_RECENT_WEEKS):
    """The Director's own explicit no-support fallback: among weeks
    STRICTLY BEFORE the most recent `exclude_recent_weeks` weeks, the
    MOST RECENT one whose own close was <= `latest_close` - i.e. "how
    long ago was price last this low, ignoring the last 8 weeks'
    normal noise". `weekly_closes` is oldest-first; scanned newest-
    first (reversed) so the first qualifying bar found IS the most
    recent one. None if nothing in that older window ever closed this
    low (the stock has never traded this low outside the recent-weeks
    window - a genuine, if unusual, outcome, never a crash)."""
    if exclude_recent_weeks <= 0:
        older_weeks = weekly_closes
    else:
        older_weeks = weekly_closes[:-exclude_recent_weeks] if len(weekly_closes) > exclude_recent_weeks else []
    for date, close in reversed(older_weeks):
        if close <= latest_close:
            return date, close
    return None, None


def compute_support_levels(weekly_dates, weekly_highs, weekly_lows, weekly_closes,
                            left=PIVOT_LEFT_BARS, right=PIVOT_RIGHT_BARS,
                            weeks_per_year=WEEKS_PER_YEAR,
                            lowest_since_exclude_recent_weeks=LOWEST_SINCE_EXCLUDE_RECENT_WEEKS):
    """The full TASK 36 result for one ticker, built from parallel
    oldest-first lists (`weekly_dates`/`weekly_highs`/`weekly_lows`/
    `weekly_closes` - same length, same index alignment, no DataFrame
    dependency so this stays trivially unit-testable). The latest bar
    (weekly_closes[-1]) is treated as "today's close" - the caller is
    responsible for making sure the LAST bar is genuinely the most
    recent available week (a partial/in-progress current week is fine
    and expected - this mirrors what the owner's own TradingView chart
    shows intraweek too).

    Returns {
      "support_price": float|None, "support_date": date|None,
      "pct_above_support": float|None,       # (latest_close/support_price - 1) * 100
      "next_support_price": float|None, "next_support_date": date|None,
      "drop_from_12m_high": float|None,      # (latest_close/high_52w - 1) * 100, negative below the high
      "has_support": bool,
      "lowest_since_date": date|None, "lowest_since_price": float|None,
    } - lowest_since_* are only ever set when has_support is False;
    support/next_support are only ever set when has_support is True.
    Returns an all-None/False "no data" shape (never raises) when
    there are too few weeks for even one confirmed pivot - the same
    "missing data never blocks a decision" philosophy as the rest of
    this codebase."""
    if not weekly_closes:
        return {
            "support_price": None, "support_date": None, "pct_above_support": None,
            "next_support_price": None, "next_support_date": None,
            "drop_from_12m_high": None, "has_support": False,
            "lowest_since_date": None, "lowest_since_price": None,
        }

    latest_close = weekly_closes[-1]
    lows_with_dates = list(zip(weekly_dates, weekly_lows))
    closes_with_dates = list(zip(weekly_dates, weekly_closes))

    pivots = confirmed_pivot_lows(lows_with_dates, left=left, right=right)
    survivors = unbroken_pivot_lows(closes_with_dates, pivots)

    eligible = sorted(
        ((date, price) for _idx, date, price in survivors if price <= latest_close),
        key=lambda dp: dp[1], reverse=True,
    )
    # De-duplicate adjacent pivots at (near-)identical price levels -
    # the same real support floor can legitimately produce more than
    # one confirmed pivot bar over the years (price revisits it
    # repeatedly); only the single nearest-to-price instance of each
    # distinct level is kept, so "support" and "next support" are two
    # genuinely DIFFERENT price floors, not the same one twice.
    _DEDUPE_PCT = 0.01
    deduped = []
    for date, price in eligible:
        if deduped and abs(price - deduped[-1][1]) <= deduped[-1][1] * _DEDUPE_PCT:
            continue
        deduped.append((date, price))

    recent_window = weekly_closes[-weeks_per_year:] if weeks_per_year else weekly_closes
    recent_highs_window = weekly_highs[-weeks_per_year:] if weeks_per_year else weekly_highs
    high_52w = max(recent_highs_window) if recent_highs_window else max(weekly_highs)
    drop_from_12m_high = ((latest_close / high_52w) - 1.0) * 100.0 if high_52w else None

    if deduped:
        support_date, support_price = deduped[0]
        next_support_date, next_support_price = (deduped[1] if len(deduped) > 1 else (None, None))
        pct_above_support = ((latest_close / support_price) - 1.0) * 100.0
        return {
            "support_price": support_price, "support_date": support_date,
            "pct_above_support": pct_above_support,
            "next_support_price": next_support_price, "next_support_date": next_support_date,
            "drop_from_12m_high": drop_from_12m_high, "has_support": True,
            "lowest_since_date": None, "lowest_since_price": None,
        }

    lowest_since_date, lowest_since_price = _lowest_since(
        closes_with_dates, latest_close, exclude_recent_weeks=lowest_since_exclude_recent_weeks,
    )
    return {
        "support_price": None, "support_date": None, "pct_above_support": None,
        "next_support_price": None, "next_support_date": None,
        "drop_from_12m_high": drop_from_12m_high, "has_support": False,
        "lowest_since_date": lowest_since_date, "lowest_since_price": lowest_since_price,
    }
