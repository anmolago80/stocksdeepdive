"""
quote_recorder.py

Trading Cost tab, Commit 1: samples ONE real bid/ask quote per ticker
per day, while the market is genuinely open, and stores it via
quote_snapshot_store.py. Yahoo has no bid/ask HISTORY, only a live
snapshot - and outside market hours that snapshot is junk (the owner-
reported example: OCL.AX after the close showed bid A$5.75 / ask
A$6.45, a 12% gap). This module exists so a real quote gets captured
once a day, every day, starting now - Commit 2's Corwin-Schultz
estimator (a separate module) fills every OTHER day from ordinary
daily high/low, and is checked against these real points.

RUNS REGARDLESS OF ENABLE_TRADING_COST - it only writes to its own
table (quote_snapshot_store.py) and never changes anything a visitor
sees. History needs to start building now so there's something to show
once the tab itself ships (Commit 3).

SCOPE: every ticker in the "ASX 200"/"S&P 500" saved scans (scan_store.
load_scan_raw), deduped within each market. Reusing the saved-scan
ticker list (rather than re-fetching scanner_engine.fetch_asx200()/
fetch_sp500() from Wikipedia a second time) means this job costs
nothing extra against Wikipedia and always matches exactly what the
site already scans nightly.

CALL COST: yfinance has no batched call that returns bid/ask -
Ticker.fast_info (the cheap batched-friendly path market_cap_engine.py
already prefers) does NOT carry bid/ask at all, and yf.download()
(this codebase's own batched OHLCV path, nightly_scan.py's reprice
pass) only returns price bars, never a quote. Ticker(...).info is the
only yfinance source with bid/ask/bidSize/askSize, so this is
necessarily one .info call per ticker - same per-ticker-network-call
shape as nightly_scan.py's own sector/attention top-up passes, reusing
their PER_TICKER_SLEEP pacing (0.5s) rather than inventing a new
throttle constant.

REJECTION (reject junk rather than store it - "a missing day is fine,
a wrong day is not"): a quote is rejected, never stored, when:
  - marketState is present and isn't "REGULAR"  -> REJECT_MARKET_NOT_REGULAR
  - marketState is ABSENT and there's no volume -> REJECT_NO_VOLUME
  - bid or ask is missing, zero, or negative    -> REJECT_MISSING
  - ask <= bid                                  -> REJECT_ASK_LE_BID
  - spread > 10% of the midpoint                -> REJECT_WIDE_SPREAD
  - last trade price > 5% from the midpoint     -> REJECT_PRICE_OFF_MID
  - bid/ask/last all equal the ticker's own
    previous stored snapshot                    -> REJECT_FROZEN_QUOTE
Every rejection is counted by reason and the count is persisted via
quote_snapshot_store.record_run_summary() (the Admin Dashboard's
rejection panel reads it back - a rejected quote itself leaves no row
anywhere else, by design).

TRADING-DAY GATE: local weekday (Mon-Fri in the target market's own
timezone) as a first, cheap filter, PLUS the market-state/volume/
frozen-quote checks above as the real gate - closing the holiday gap
the weekday check alone leaves open, WITHOUT a market-holiday calendar
(there still isn't one anywhere in this codebase - grepped; no
pandas_market_calendars/exchange_calendars/holidays dependency, no
existing helper - and none is needed): yfinance's own .info dict
already carries the market's real state.
  1. marketState ("REGULAR"/"CLOSED"/"PRE"/"POST"/"POSTPOST", when
     present) is the authoritative signal - only a "REGULAR" quote is
     ever stored.
  2. When marketState is absent (yfinance doesn't always return it),
     fall back to that day's volume (regularMarketVolume, or volume) -
     zero or missing means nothing has traded today, holiday or not.
  3. Belt and braces, in case a rare response reports "REGULAR" with
     real volume but Yahoo actually just replayed the exact prior
     session's numbers unchanged (seen in the wild often enough to
     guard against directly rather than trust 1/2 alone): reject when
     bid, ask AND last price are all identical to the ticker's own
     previously stored snapshot (quote_snapshot_store.latest_snapshot)
     - a quote that hasn't moved a single cent from the last real one
       is a replay, not a second independent trading session.

FAIL-OPEN: every per-ticker fetch is individually try/except-guarded
(one bad ticker never stops the rest of the run, same convention as
nightly_scan.py's per-ticker loops); the two run_*_recorder() entry
points let a genuine job-level exception propagate to scheduler_engine.
py's own per-job try/except (matching _run_watchdog's own "let it
raise so the retry-cap sees a real failure" contract) - either way, an
error here can never affect any page or the nightly scan, which never
call into this module at all.
"""

import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import yfinance as yf

import quote_snapshot_store
import scan_store

MARKET_ASX = "ASX"
MARKET_US = "US"

_MARKET_UNIVERSE = {MARKET_ASX: "ASX 200", MARKET_US: "S&P 500"}
_MARKET_TZ = {MARKET_ASX: ZoneInfo("Australia/Sydney"), MARKET_US: ZoneInfo("America/New_York")}
_MARKET_CURRENCY_FALLBACK = {MARKET_ASX: "AUD", MARKET_US: "USD"}

# Same pacing as nightly_scan.py's own sector/attention top-up passes -
# no reason for this job's per-ticker .info calls to hit Yahoo any
# harder than every other per-ticker loop already does.
PER_TICKER_SLEEP = 0.5

REJECT_MISSING = "rejected_missing_or_nonpositive"
REJECT_ASK_LE_BID = "rejected_ask_le_bid"
REJECT_WIDE_SPREAD = "rejected_wide_spread"
REJECT_PRICE_OFF_MID = "rejected_price_off_mid"
REJECT_MARKET_NOT_REGULAR = "rejected_market_not_regular"
REJECT_NO_VOLUME = "rejected_no_volume"
REJECT_FROZEN_QUOTE = "rejected_frozen_quote"

_MAX_SPREAD_PCT_OF_MID = 0.10
_MAX_PRICE_OFFSET_PCT_OF_MID = 0.05

_REGULAR_MARKET_STATE = "REGULAR"

# Mid-session sampling window, in the target market's OWN local time -
# 13:00 is the spec's own chosen instant; the upper bound (16:00) is a
# safety bound only, so a process that comes back up late (a Railway
# redeploy, a missed tick) doesn't record what's really the CLOSING
# quote and label it a mid-session sample - both ASX and NYSE happen to
# close at 16:00 their own local time, which is what sets this bound.
WINDOW_START_HOUR = 13
WINDOW_END_HOUR = 16


def _get_info_field(info, *keys):
    """First non-None value found under any of `keys` in a yfinance
    .info dict - same defensive multi-key-fallback style as
    fundamentals_data.py's _fresh_price() (yfinance's own field names
    have drifted release to release before)."""
    for k in keys:
        v = info.get(k)
        if v is not None:
            return v
    return None


def is_due_now(market, now=None):
    """True if `market` (MARKET_ASX/MARKET_US) is inside today's
    sampling window in ITS OWN local time - a weekday, and local hour
    in [WINDOW_START_HOUR, WINDOW_END_HOUR). `now` is injectable (UTC
    or any tz-aware datetime) for testing; defaults to the real current
    time. Pure - no I/O, no state - the scheduler's own state-file date
    guard (mirroring every other daily job in scheduler_engine.py) is
    what actually prevents firing twice in one day."""
    now = now or datetime.now(timezone.utc)
    local = now.astimezone(_MARKET_TZ[market])
    if local.weekday() >= 5:  # Sat/Sun
        return False
    return WINDOW_START_HOUR <= local.hour < WINDOW_END_HOUR


def tickers_for_market(market):
    """Deduped, sorted ticker list for `market`, read from the saved
    ASX 200 / S&P 500 scan (scan_store.load_scan_raw - skips the 72h
    staleness cutoff load_scan() applies, since a slightly-stale ticker
    LIST is still a fine list to sample quotes for; only the quote
    itself needs to be fresh). [] if that scan has never been saved."""
    payload = scan_store.load_scan_raw(_MARKET_UNIVERSE[market])
    if not payload or not payload.get("rows"):
        return []
    tickers = {(row.get("Ticker") or "").strip() for row in payload["rows"]}
    tickers.discard("")
    return sorted(tickers)


def _classify(bid, ask, last_price):
    """(reject_reason_or_None, midpoint_or_None) for one ticker's raw
    .info fields, against the four rejection rules in this module's own
    docstring. A quote with no rejection reason is exactly the quote
    this job stores."""
    if not (isinstance(bid, (int, float)) and isinstance(ask, (int, float))
            and bid > 0 and ask > 0):
        return REJECT_MISSING, None
    if ask <= bid:
        return REJECT_ASK_LE_BID, None
    midpoint = (bid + ask) / 2.0
    spread_pct = (ask - bid) / midpoint
    if spread_pct > _MAX_SPREAD_PCT_OF_MID:
        return REJECT_WIDE_SPREAD, None
    if isinstance(last_price, (int, float)) and last_price > 0:
        offset_pct = abs(last_price - midpoint) / midpoint
        if offset_pct > _MAX_PRICE_OFFSET_PCT_OF_MID:
            return REJECT_PRICE_OFF_MID, None
    return None, midpoint


def _classify_market_state(market_state, volume):
    """Holiday-gap follow-up: reject_reason_or_None from yfinance's own
    market-state/volume signals, checked BEFORE the four bid/ask rules
    above (there's no point classifying a spread that was never sampled
    during real trading). Pure - no I/O.

    marketState present and not "REGULAR" -> REJECT_MARKET_NOT_REGULAR
    (a holiday's own response is typically "CLOSED", same as any
    regular after-hours check - this one signal covers both).
    marketState absent -> fall back to the day's volume; zero/missing
    -> REJECT_NO_VOLUME. marketState present and "REGULAR" -> volume is
    not re-checked here (a thin, low-liquidity ticker can legitimately
    have very low volume during a real regular session; the point of
    this fallback is only to stand in for marketState when Yahoo
    doesn't supply it)."""
    if market_state is not None:
        if market_state != _REGULAR_MARKET_STATE:
            return REJECT_MARKET_NOT_REGULAR
        return None
    if not (isinstance(volume, (int, float)) and volume > 0):
        return REJECT_NO_VOLUME
    return None


def _is_frozen(bid, ask, last_price, prev):
    """Holiday-gap follow-up, belt and braces: True if bid/ask/last_
    price are ALL identical to `prev` (a {"bid","ask","last_price"}
    dict from quote_snapshot_store.latest_snapshot(), or None if this
    ticker has no earlier snapshot at all - never frozen with nothing
    to compare against). Pure - `prev` is passed in rather than looked
    up here, so this stays testable without touching the DB."""
    if not prev:
        return False
    return (bid == prev.get("bid") and ask == prev.get("ask")
            and last_price == prev.get("last_price"))


def _record_market(market, log=print):
    """The actual recording pass for one market - every per-ticker step
    individually guarded (a bad ticker is logged and skipped, never
    raised), so a genuine exception escaping this function means
    something is wrong with the run as a whole (e.g. scan_store itself
    broken), not with any one ticker - exactly the case scheduler_
    engine.py's retry-cap wants to see as a real failure. Returns
    (captured_count, rejection_counts dict) and persists both a
    quote_snapshot_runs summary row and, per accepted ticker, a
    quote_snapshots row."""
    tickers = tickers_for_market(market)
    now_utc = datetime.now(timezone.utc)
    local_date = now_utc.astimezone(_MARKET_TZ[market]).strftime("%Y-%m-%d")
    currency_fallback = _MARKET_CURRENCY_FALLBACK[market]

    captured = 0
    counts = {
        REJECT_MISSING: 0, REJECT_ASK_LE_BID: 0, REJECT_WIDE_SPREAD: 0,
        REJECT_PRICE_OFF_MID: 0, REJECT_MARKET_NOT_REGULAR: 0,
        REJECT_NO_VOLUME: 0, REJECT_FROZEN_QUOTE: 0,
    }

    log(f"[quote_recorder] {market}: sampling {len(tickers)} ticker(s) for {local_date}")
    for i, ticker in enumerate(tickers):
        try:
            info = yf.Ticker(ticker).info or {}
            bid = _get_info_field(info, "bid")
            ask = _get_info_field(info, "ask")
            last_price = _get_info_field(info, "regularMarketPrice", "currentPrice")
            market_state = _get_info_field(info, "marketState")
            volume = _get_info_field(info, "regularMarketVolume", "volume")

            reason = _classify_market_state(market_state, volume)
            if reason is None:
                reason, _midpoint = _classify(bid, ask, last_price)
            if reason is None and _is_frozen(
                    bid, ask, last_price, quote_snapshot_store.latest_snapshot(ticker)):
                reason = REJECT_FROZEN_QUOTE

            if reason is not None:
                counts[reason] += 1
            else:
                quote_snapshot_store.record_snapshot(
                    ticker=ticker,
                    snap_date=local_date,
                    snap_at_utc=now_utc.isoformat(),
                    bid=float(bid),
                    ask=float(ask),
                    bid_size=_get_info_field(info, "bidSize"),
                    ask_size=_get_info_field(info, "askSize"),
                    last_price=float(last_price) if isinstance(last_price, (int, float)) else None,
                    currency=_get_info_field(info, "currency") or currency_fallback,
                    source="yfinance.info",
                )
                captured += 1
        except Exception as e:
            log(f"[quote_recorder] {market}/{ticker}: failed, skipped ({e})")
        if i + 1 < len(tickers):
            time.sleep(PER_TICKER_SLEEP)

    total_rejected = sum(counts.values())
    _reasons = ", ".join(f"{k}={v}" for k, v in counts.items() if v)
    _suffix = f" ({_reasons})" if _reasons else ""
    log(f"[quote_recorder] {market}: captured {captured}, rejected {total_rejected}{_suffix}")
    quote_snapshot_store.record_run_summary(
        run_date=local_date, market=market, captured_count=captured,
        rejection_counts=counts, ran_at_utc=now_utc.isoformat(),
    )
    return captured, counts


def run_asx_recorder(log=print):
    """scheduler_engine.py entry point for the ASX-local sampling slot.
    Deferred yfinance/network work only happens when this is actually
    called, same "don't slow every scheduler tick" convention as every
    _run_* job in that module."""
    _record_market(MARKET_ASX, log=log)


def run_us_recorder(log=print):
    """scheduler_engine.py entry point for the US-local sampling slot."""
    _record_market(MARKET_US, log=log)
