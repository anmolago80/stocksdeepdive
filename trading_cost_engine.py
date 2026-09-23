"""
trading_cost_engine.py

Trading Cost tab, Commit 2: the estimator (Corwin & Schultz's 2-day
high-low bid-ask spread estimator) and the pure per-ticker series/
summary behind the tab. Deliberately self-contained - no Streamlit, no
network, no pandas, no database - same shape as property_vs_index_
engine.py: every input is a plain value/list/dict the caller already
has, every output is a plain value/list/dict. app.py (Commit 3) is the
one place that touches Streamlit or a live/cached fetch or the
quote_snapshots table; this module never does.

WHY AN ESTIMATE AT ALL (see quote_recorder.py's own module docstring
for the recorder side, Commit 1): Yahoo has no bid/ask HISTORY, only a
live snapshot - Commit 1 started recording one real quote a day going
forward, but every day BEFORE that recorder existed (and any day it
missed) has no real quote to show. Corwin & Schultz (2012) estimate
the bid-ask spread from nothing but a stock's own daily high/low - no
bid/ask data needed at all - so this fills every day, including the
entire past, from price history this site already has cached.

THE ESTIMATOR: corwin_schultz_spread(highs, lows) - the standard 2-day
high-low estimator, applied to each pair of CONSECUTIVE days (t, t+1):
  beta  = ln(H_t/L_t)^2 + ln(H_t+1/L_t+1)^2
  gamma = ln(max(H_t,H_t+1) / min(L_t,L_t+1))^2
  k     = 3 - 2*sqrt(2)
  alpha = (sqrt(2*beta) - sqrt(beta))/k - sqrt(gamma/k)
  S     = 2*(exp(alpha) - 1) / (1 + exp(alpha))
A negative S (alpha comes out negative when the 2-day range is barely
wider than either single day's own range on its own - noise, not a
real spread signal) is reported as 0.0, never as a negative spread.
The estimate for day i is computed from the pair (day i-1, day i) and
reported AS DAY i's own estimate - so the very FIRST day in any
highs/lows array has nothing to pair with and gets None, not 0.0 -
None means "not computed", 0.0 means "computed, and it came out
non-positive" - two different things, never conflated.

SPREAD UNITS: every spread value in this module (recorded, estimated,
band thresholds) is a PERCENTAGE (e.g. 0.42 means 0.42%, not 0.0042) -
picked once here, up front, since the band thresholds in the spec are
themselves literal percentages (tight < 0.5%, wide > 1.5%), avoiding a
fraction<->percentage conversion at every call site.

MINOR-UNIT (cents/pence) PRICING: doesn't apply to this site today -
grepped for GBp/pence/minor-unit handling anywhere in the codebase (an
earlier research pass for this same task did the same, independently)
and found none; ASX 200 trades in AUD, S&P 500 in USD, neither a minor
unit. Noted here rather than silently assumed, per the task's own
question. It would not actually matter for THIS module even if it did
apply: every spread computed here is a PERCENTAGE of price (bid-ask
over midpoint, or the CS estimator's own log-ratio formula, which is
scale-invariant by construction - ln(H/L) is identical whether H and L
are both in dollars or both in cents) - as long as one ticker's own
bid/ask/high/low/close all share ONE consistent unit (which they
always will, coming from the same yfinance response), no rescaling
step is needed anywhere in this module.

DESIGN DEVIATION FROM THE TASK'S OWN trading_cost_series(ticker,
days=30) SIGNATURE, flagged explicitly rather than silently changed:
this function instead takes trading_cost_series(ticker, price_history,
snapshots, days=30) - `price_history`/`snapshots` are plain lists the
CALLER fetches (app.py's own get_price_history() cache, quote_
snapshot_store.snapshots_for_ticker()) and passes straight through,
rather than this module reaching into either of those on its own. This
keeps the engine at true zero I/O - fully unit-testable with plain
Python literals, no mocking of Streamlit's cache or a SQLite file
needed - matching property_vs_index_engine.py's own explicit
precedent (the task's own words: "same shape as property_vs_index_
engine.py"), and the same "reads and writes are the caller's problem,
the engine only computes" boundary moat_engine.compute_moat_dry_run()'s
own `bundle=` parameter already established for this codebase (Commit
S, 21 Sep 2026).
"""

import math

BAND_TIGHT = "tight"
BAND_MODERATE = "moderate"
BAND_WIDE = "wide"

# Named (not inline-literal) so Commit 3's own "wide above X%" dashed
# threshold line on the spread chart reads these directly rather than
# duplicating the numbers - the chart and spread_band() can never drift
# apart out of sync.
TIGHT_THRESHOLD_PCT = 0.5
WIDE_THRESHOLD_PCT = 1.5

_K = 3 - 2 * math.sqrt(2)


def _pair_spread(h_t, l_t, h_t1, l_t1):
    """Corwin-Schultz S for ONE consecutive pair of (high, low) days -
    see this module's own docstring for the formula. Returns a
    PERCENTAGE (this module's own unit), never negative - a negative
    alpha means this pair's own 2-day range gave no real spread
    signal, reported as "no spread detected" (0.0), not a negative
    number."""
    beta = math.log(h_t / l_t) ** 2 + math.log(h_t1 / l_t1) ** 2
    gamma = math.log(max(h_t, h_t1) / min(l_t, l_t1)) ** 2
    alpha = (math.sqrt(2 * beta) - math.sqrt(beta)) / _K - math.sqrt(gamma / _K)
    s = 2 * (math.exp(alpha) - 1) / (1 + math.exp(alpha))
    return max(s, 0.0) * 100.0


def corwin_schultz_spread(highs, lows):
    """[None, S_1, S_2, ..., S_{n-1}] for `highs`/`lows` (equal-length,
    same order, one point per day) - S_i is the pair-spread for (day
    i-1, day i), as a PERCENTAGE (this module's own unit). Index 0 is
    None (no earlier day to pair with) - see _pair_spread()'s own
    docstring for why a computed-but-non-positive day is 0.0 instead, a
    different case entirely."""
    n = len(highs)
    if n != len(lows):
        raise ValueError("highs and lows must be the same length")
    out = [None] * n
    for i in range(1, n):
        out[i] = _pair_spread(highs[i - 1], lows[i - 1], highs[i], lows[i])
    return out


def spread_band(spread_pct):
    """"tight" (<0.5%), "moderate" (0.5%-1.5% inclusive), "wide"
    (>1.5%) - the spec's own three bands, spread_pct in this module's
    own percentage unit. None in, None out (nothing to band)."""
    if spread_pct is None:
        return None
    if spread_pct < TIGHT_THRESHOLD_PCT:
        return BAND_TIGHT
    if spread_pct <= WIDE_THRESHOLD_PCT:
        return BAND_MODERATE
    return BAND_WIDE


def _recorded_spread_pct(bid, ask):
    """(ask-bid)/midpoint, as a PERCENTAGE - None if bid/ask aren't
    both a real positive number. quote_recorder.py's own rejection
    rules mean a row it actually stored always has both, but this
    stays defensive since `snapshots` here is caller-supplied (e.g.
    hand-built in a test)."""
    if not (isinstance(bid, (int, float)) and isinstance(ask, (int, float))
            and bid > 0 and ask > 0):
        return None
    midpoint = (bid + ask) / 2.0
    return (ask - bid) / midpoint * 100.0


def _median(values):
    if not values:
        return None
    s = sorted(values)
    n = len(s)
    mid = n // 2
    if n % 2:
        return s[mid]
    return (s[mid - 1] + s[mid]) / 2.0


def trading_cost_series(ticker, price_history, snapshots, days=30):
    """The Trading Cost tab's per-ticker data, entirely from what the
    caller already has cached/stored - see this module's own docstring
    for why the signature takes `price_history`/`snapshots` explicitly
    rather than fetching them itself.

    `price_history`: [{"date","high","low","close","volume"}, ...], any
    order, any length >= 0 (app.py's own get_price_history() cache
    already returns ~6 months of daily bars - far more than `days`).
    This function does its own windowing, using ONE EXTRA day of
    lookback beyond `days` (when the caller's history reaches back
    that far) so every day in the OUTPUT window gets a real Corwin-
    Schultz estimate - only the very first day of the CALLER'S entire
    history is ever left with no estimate (see corwin_schultz_spread()
    's own docstring), and only when the window reaches all the way
    back to it (i.e. the caller supplied fewer than `days`+1 rows).

    `snapshots`: [{"snap_date","bid","ask"}, ...] for this ticker, ANY
    length/date range the caller has - NOT pre-windowed to `days`.
    "recording_start_date" below is the EARLIEST date across ALL of
    `snapshots`, which would be wrong if the caller pre-truncated it -
    quote_snapshot_store.snapshots_for_ticker() (Commit 2's own new
    store function) returns the ticker's FULL history for exactly this
    reason.

    Returns {"ticker", "days": [{"date","close","recorded_bid",
    "recorded_ask","recorded_spread_pct","estimated_spread_pct"}, ...]
    (ascending by date, length = min(days, len(price_history))),
    "median_recorded_spread_pct", "average_estimated_spread_pct",
    "recording_start_date" (None if `snapshots` is empty),
    "recorded_days_count",
    "avg_daily_value_traded_30d" (close*volume, averaged over the SAME
    `days` window as everything else here - named for the default,
    scales with `days` like every other figure in this function, not
    hardcoded to a literal 30)}."""
    recording_start_date = min((s["snap_date"] for s in snapshots), default=None)

    if not price_history:
        return {
            "ticker": ticker, "days": [],
            "median_recorded_spread_pct": None,
            "average_estimated_spread_pct": None,
            "recording_start_date": recording_start_date,
            "recorded_days_count": 0,
            "avg_daily_value_traded_30d": None,
        }

    history = sorted(price_history, key=lambda r: r["date"])
    window = history[-(days + 1):]  # one extra lookback day, when available
    highs = [r["high"] for r in window]
    lows = [r["low"] for r in window]
    estimates = corwin_schultz_spread(highs, lows)
    # Drop the lookback day itself from the OUTPUT - it exists only to
    # give the first real output day a predecessor to pair with. If the
    # caller's own history is shorter than days+1, there IS no separate
    # lookback day, so nothing is dropped: every available row (and its
    # corwin_schultz_spread() estimate, index 0 = None included) is
    # exactly what's returned.
    if len(window) > days:
        window, estimates = window[1:], estimates[1:]

    snaps_by_date = {s["snap_date"]: s for s in snapshots}

    rows = []
    for row, est in zip(window, estimates):
        snap = snaps_by_date.get(row["date"])
        recorded_bid = snap["bid"] if snap else None
        recorded_ask = snap["ask"] if snap else None
        rows.append({
            "date": row["date"],
            "close": row.get("close"),
            "recorded_bid": recorded_bid,
            "recorded_ask": recorded_ask,
            "recorded_spread_pct": _recorded_spread_pct(recorded_bid, recorded_ask),
            "estimated_spread_pct": est,
        })

    recorded_spreads = [r["recorded_spread_pct"] for r in rows if r["recorded_spread_pct"] is not None]
    estimated_spreads = [r["estimated_spread_pct"] for r in rows if r["estimated_spread_pct"] is not None]
    values_traded = [
        row["close"] * row["volume"] for row in window
        if isinstance(row.get("close"), (int, float)) and isinstance(row.get("volume"), (int, float))
    ]

    return {
        "ticker": ticker,
        "days": rows,
        "median_recorded_spread_pct": _median(recorded_spreads),
        "average_estimated_spread_pct": (
            sum(estimated_spreads) / len(estimated_spreads) if estimated_spreads else None
        ),
        "recording_start_date": recording_start_date,
        "recorded_days_count": len(recorded_spreads),
        "avg_daily_value_traded_30d": (
            sum(values_traded) / len(values_traded) if values_traded else None
        ),
    }
