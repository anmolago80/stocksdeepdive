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

SCOPE (widened, Sep 2026 - tiered roster): the ROSTER (every ticker
eligible to ever be recorded - see roster_tickers() below) is every
ticker in the "ASX 200"/"S&P 500" saved scans (scan_store.
load_scan_raw, deduped within each market - reusing the saved-scan
ticker list, rather than re-fetching scanner_engine.fetch_asx200()/
fetch_sp500() from Wikipedia a second time, means this job costs
nothing extra against Wikipedia and always matches exactly what the
site already scans nightly) PLUS every hand-covered Research ticker
(blog_render._covered_tickers()) and every ticker the author currently
holds a disclosed position in (positions_store.all_positions(),
status == "holds"). A single recording RUN doesn't sample the whole
roster every day, though: it samples today_roster_tickers(market) -
the DAILY tier (every research/held ticker, plus any roster ticker
whose most recent recorded spread exceeded 1%, so a wide reading self-
promotes it starting the very next run) union today's own rotating
1/5th slice of the WEEKLY tier (everything else) - see daily_tier_
tickers()/weekly_tier_tickers()/today_weekly_slice() below for the
full tiering logic.

CALL COST (rewritten, Sep 2026 - batch optimisation): Ticker.fast_info
(the cheap batched-friendly path market_cap_engine.py already prefers)
still does NOT carry bid/ask at all, and yf.download() (this
codebase's own batched OHLCV path, nightly_scan.py's reprice pass)
still only returns price bars, never a quote - both of those earlier
findings still hold. What changed: this job no longer calls
Ticker(...).info (the HEAVY per-ticker quoteSummary endpoint) at all.
It now hits Yahoo's own light multi-symbol quote endpoint (v7/finance/
quote - the SAME light bid/ask/last/volume/marketState fields
Ticker.info's own bid/ask ultimately come from, just without the rest
of quoteSummary's heavy payload) in chunks of up to _BULK_CHUNK_SIZE
(100) symbols per request - see _fetch_quotes_bulk() below. A symbol
the bulk response doesn't cover falls back to one INDIVIDUAL request
against the SAME light endpoint (_fetch_quote_single()) - never back
to the heavy .info call this whole change exists to get away from.
Every ticker a quality gate rejects on its first pass gets exactly one
retry, later in the same run (see _record_market()'s own retry pass) -
conditions inside the sampling window can genuinely shift (a market
that just opened, a momentarily frozen quote).

Implementation note, flagged rather than silently relied on: the
bulk/individual fetchers both go through yfinance.data.YfData - that
library's own internal cookie/crumb-handling session, the same
machinery Ticker.info itself uses under the hood (a bare unauthenticated
requests.get() gets rejected by Yahoo). This is yfinance's PRIVATE
implementation detail, not its public Ticker/download() API surface,
and could move or rename between yfinance releases - both fetchers
fail open (caught, logged, treated as "no quote for this ticker" - see
_fetch_quotes_bulk()'s own docstring) rather than raising, so a future
yfinance internals change degrades this job to "nothing captured that
run", never a crash.

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

import quote_snapshot_store
import scan_store
import trading_cost_engine

MARKET_ASX = "ASX"
MARKET_US = "US"

_MARKET_UNIVERSE = {MARKET_ASX: "ASX 200", MARKET_US: "S&P 500"}
_MARKET_TZ = {MARKET_ASX: ZoneInfo("Australia/Sydney"), MARKET_US: ZoneInfo("America/New_York")}
_MARKET_CURRENCY_FALLBACK = {MARKET_ASX: "AUD", MARKET_US: "USD"}

# Same pacing as nightly_scan.py's own sector/attention top-up passes -
# reused for the two remaining PER-TICKER loops this job still has
# (the individual-fallback pass for a symbol bulk didn't cover, and
# the same fallback inside the one retry pass) - no reason for either
# to hit Yahoo any harder than every other per-ticker loop already does.
PER_TICKER_SLEEP = 0.5

# Batch optimisation (Sep 2026): Yahoo's own practical cap on symbols
# per v7/finance/quote request - comfortably covers a full ASX 200/
# S&P 500 pass in 2-3 requests instead of 200-500 individual .info
# calls. _BULK_REQUEST_PAUSE is the polite pause BETWEEN those chunk
# requests (replacing the old per-ticker sleep at the chunk level, now
# that most tickers travel in the same request) and also doubles as
# the pause before the one retry pass below starts.
_BULK_CHUNK_SIZE = 100
_BULK_REQUEST_PAUSE = 1.0
_QUOTE_ENDPOINT_PATH = "/v7/finance/quote"

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


def _research_and_position_tickers():
    """Hand-covered Research tickers (blog_render._covered_tickers() -
    reused as-is, this codebase's own established precedent for
    "a private-by-convention function already has the exact logic
    needed, import and reuse it" - it already reads compounder_data.
    json's own "tickers" dict, fail-open to an empty set) UNIONED with
    the author's own CURRENT positions (positions_store.all_positions(),
    status == "holds" only - "never"/"closed" rows are disclosures
    about NOT currently holding something, not a reason to prioritise
    its quote recording). Deferred imports: neither blog_render nor
    positions_store is a circular import here, but neither needs to be
    a hard top-level dependency of this module either (same reasoning
    compounder_ui.py already applies to its own paywall_engine/ai_gate
    imports). Fails open to whatever half succeeded - a broken
    positions_store read never means blog_render's own tickers are
    lost too, and vice versa."""
    tickers = set()
    try:
        import blog_render
        tickers |= blog_render._covered_tickers()
    except Exception:
        pass
    try:
        import positions_store
        tickers |= {
            t.strip().upper() for t, row in positions_store.all_positions().items()
            if (row.get("status") or "").lower() == "holds"
        }
    except Exception:
        pass
    return tickers


def roster_tickers(market=None):
    """The ticker set currently eligible for daily quote recording -
    tickers_for_market()'s own scanned-universe list UNIONED with
    _research_and_position_tickers() above (Commit 4's own words:
    "Roster = every scanned universe member plus ALL hand-covered
    Research tickers and author-position tickers"). `market`:
    MARKET_ASX/MARKET_US for one market's own SCANNED-UNIVERSE roster
    only (the research/position addition isn't market-scoped, so it's
    never added here - a caller wanting the true combined roster for
    one market's own recording pass uses today_roster_tickers() below,
    not this with a `market` argument), or None (default) for the
    full combined roster the Trading Cost tab's own per-ticker honesty
    check (compounder_ui.render_trading_cost_tab(), Commit 2) reads -
    that check widening automatically, with no code change on its own
    side, is exactly why Commit 2's own non-roster message now only
    ever appears for a ticker genuinely outside every universe. A SET
    (not the sorted list tickers_for_market() returns) - this function
    exists for fast membership checks, not display."""
    if market is not None:
        return set(tickers_for_market(market))
    universe = set(tickers_for_market(MARKET_ASX)) | set(tickers_for_market(MARKET_US))
    return universe | _research_and_position_tickers()


def is_in_roster(ticker):
    """True if `ticker` is (or will be, from the next run) sampled by
    the daily quote recorder - see roster_tickers() above. Matched
    case-insensitively (tickers_for_market()'s own stored casing is
    whatever the scan itself used - never assumed to already match the
    caller's own casing). False for an empty/None ticker, never a
    lookup error."""
    if not ticker:
        return False
    return ticker.strip().upper() in {t.upper() for t in roster_tickers()}


# Tiered roster (Commit 4, Sep 2026): the DAILY tier is every research/
# held ticker (regardless of its own spread) plus any OTHER roster
# ticker whose most recently RECORDED spread exceeded this threshold -
# a wide reading is exactly the case this whole feature exists to
# track closely, so it earns daily sampling from the very next run.
# Everything else is the WEEKLY tier, sampled via a rotating 1/5th
# per trading day (_WEEKDAY_ROTATION_SIZE) rather than daily.
DAILY_MIN_SPREAD_PCT = 1.0
_WEEKDAY_ROTATION_SIZE = 5


def daily_tier_tickers(market=None):
    """Tickers sampled EVERY trading day: every research/held ticker
    (_research_and_position_tickers(), regardless of market - the task
    's own words: "Daily tier: research/held tickers plus...") that is
    ALSO on this market's own roster, plus any roster ticker whose most
    recent quote_snapshot_store.latest_snapshot() spread exceeded
    DAILY_MIN_SPREAD_PCT. Recomputed from STORED snapshots on every
    call, never a separately persisted "is daily" flag - a single wide
    recording self-promotes a ticker to daily starting the very next
    run this function is called from, and a ticker that started wide
    and has since tightened simply stops qualifying on its own, no
    separate "demote" step needed. `market`: MARKET_ASX/MARKET_US to
    scope to one market's roster, or None for the combined roster."""
    roster = roster_tickers(market)
    tier = _research_and_position_tickers() & roster
    for ticker in roster - tier:
        latest = quote_snapshot_store.latest_snapshot(ticker)
        if not latest or latest.get("bid") is None or latest.get("ask") is None:
            continue
        spread_pct = trading_cost_engine._recorded_spread_pct(latest["bid"], latest["ask"])
        if spread_pct is not None and spread_pct > DAILY_MIN_SPREAD_PCT:
            tier.add(ticker)
    return tier


def weekly_tier_tickers(market=None):
    """Every roster ticker NOT in the daily tier - see daily_tier_
    tickers() above. Sampled via today_weekly_slice() below's rotating
    1/5th-per-trading-day, never all at once, never daily."""
    roster = roster_tickers(market)
    return roster - daily_tier_tickers(market)


def today_weekly_slice(market=None, today=None):
    """This trading day's own 1/5th slice of the weekly tier - a
    stable, deterministic rotation keyed off each ticker's own sorted
    position in the CURRENT weekly tier and today's ISO weekday
    (Monday=0 ... Sunday=6, taken mod _WEEKDAY_ROTATION_SIZE so this
    stays well-defined even called on a weekend, though the scheduler
    itself only ever runs this on a trading day). Recomputed fresh
    from the current weekly tier on every call, exactly like the daily
    tier above - a ticker that self-promotes to daily simply stops
    appearing in this rotation from its very next run, no separate
    bookkeeping to keep in sync. `today`: override for tests (a
    date instance); defaults to today's UTC date."""
    weekly = sorted(weekly_tier_tickers(market))
    if not weekly:
        return set()
    day = today if today is not None else datetime.now(timezone.utc).date()
    slot = day.weekday() % _WEEKDAY_ROTATION_SIZE
    return {t for i, t in enumerate(weekly) if i % _WEEKDAY_ROTATION_SIZE == slot}


def today_roster_tickers(market, today=None):
    """The full set of tickers _record_market() below actually samples
    TODAY for `market`: its own daily tier (every day) union today's
    own rotating weekly slice - daily_tier_tickers()/today_weekly_
    slice() above's own combination, the one place that decides what
    a single recording run covers."""
    return daily_tier_tickers(market) | today_weekly_slice(market, today=today)


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


def _chunked(seq, size):
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


def _quote_light_dict(raw):
    """One Yahoo v7/finance/quote result row -> the SAME field-name
    shape _get_info_field()/_classify()/_classify_market_state()/
    _is_frozen() above already read from a yf.Ticker(...).info dict -
    bid/ask/regularMarketPrice/regularMarketVolume/marketState/
    bidSize/askSize/currency. Yahoo's quote endpoint and .info's own
    quoteSummary both ultimately source these particular fields from
    the same underlying Yahoo data, so no renaming/translation layer
    is needed - every existing quality-gate function reads this
    exactly as it always read an .info dict, byte-for-byte unchanged."""
    return {
        "bid": raw.get("bid"),
        "ask": raw.get("ask"),
        "regularMarketPrice": raw.get("regularMarketPrice"),
        "regularMarketVolume": raw.get("regularMarketVolume"),
        "marketState": raw.get("marketState"),
        "bidSize": raw.get("bidSize"),
        "askSize": raw.get("askSize"),
        "currency": raw.get("currency"),
    }


def _yf_data_get(params, log=print):
    """One request against Yahoo's light quote endpoint via yfinance's
    own internal session (yfinance.data.YfData - see this module's own
    docstring, CALL COST section, for why: a bare unauthenticated
    request gets rejected by Yahoo, and this is the same cookie/
    crumb-handling machinery Ticker.info itself relies on). Returns the
    parsed JSON's "quoteResponse.result" list, or [] on ANY failure
    (import, network, auth, malformed JSON) - fails open, never raises,
    so a problem here degrades to "treat every symbol in this request
    as uncovered", handled entirely by the caller."""
    try:
        from yfinance.data import YfData
        from yfinance.const import _QUERY1_URL_
        resp = YfData().get(f"{_QUERY1_URL_}{_QUOTE_ENDPOINT_PATH}", params=params)
        payload = resp.json()
        return ((payload.get("quoteResponse") or {}).get("result")) or []
    except Exception as e:
        log(f"[quote_recorder] light quote request failed: {e}")
        return []


def _fetch_quotes_bulk(tickers, log=print):
    """Yahoo's multi-symbol light quote endpoint, _BULK_CHUNK_SIZE
    symbols per request, replacing one HEAVY yf.Ticker(t).info call per
    ticker with a handful of light requests for the whole market.
    Returns (quotes: {ticker: light_info_dict, ...}, missing:
    [ticker, ...]) - `missing` is every ticker whose symbol the
    endpoint's response simply didn't include (a symbol it doesn't
    cover) OR whose whole chunk request failed outright (_yf_data_get()
    already logged why) - either way, the caller falls back to one
    INDIVIDUAL light request per missing ticker (_fetch_quote_single()
    below), never the heavy .info call this whole change exists to get
    away from."""
    quotes = {}
    missing = []
    chunks = list(_chunked(tickers, _BULK_CHUNK_SIZE))
    for i, chunk in enumerate(chunks):
        rows = _yf_data_get({"symbols": ",".join(chunk)}, log=log)
        seen = set()
        for row in rows:
            sym = row.get("symbol")
            if sym:
                quotes[sym] = _quote_light_dict(row)
                seen.add(sym)
        missing.extend(t for t in chunk if t not in seen)
        if i + 1 < len(chunks):
            time.sleep(_BULK_REQUEST_PAUSE)
    return quotes, missing


def _fetch_quote_single(ticker, log=print):
    """One ticker through the SAME light quote endpoint
    _fetch_quotes_bulk() above uses, individually - the fallback for a
    symbol the bulk endpoint's response didn't cover. Deliberately NOT
    yf.Ticker(ticker).info (the heavy quoteSummary call this whole
    change exists to get away from) - "fast_info-class" in the task's
    own words: a fast, light call, even though yfinance's own actual
    FastInfo object has no bid/ask property at all (checked directly
    against this environment's installed yfinance 1.7.0 -
    yfinance/scrapers/quote.py's own FastInfo class) and so could never
    serve this job regardless of which call this fallback used. None
    on any failure or a response with no matching symbol - the caller
    already treats a None quote as "still missing" (REJECT_MISSING, the
    same gate every other ticker goes through, never a special case)."""
    rows = _yf_data_get({"symbols": ticker}, log=log)
    for row in rows:
        if row.get("symbol") == ticker:
            return _quote_light_dict(row)
    return None


def _fetch_quotes_individually(tickers, log=print):
    """`tickers` through _fetch_quote_single() above, one at a time,
    PER_TICKER_SLEEP between requests (this job's existing per-ticker
    pacing, reused rather than a second throttle constant) - the
    fallback loop both _record_market()'s first pass and its one retry
    pass call for whatever _fetch_quotes_bulk() left uncovered."""
    quotes = {}
    for i, ticker in enumerate(tickers):
        q = _fetch_quote_single(ticker, log=log)
        if q is not None:
            quotes[ticker] = q
        if i + 1 < len(tickers):
            time.sleep(PER_TICKER_SLEEP)
    return quotes


def _record_market(market, log=print):
    """The actual recording pass for one market - every per-ticker step
    individually guarded (a bad ticker is logged and skipped, never
    raised), so a genuine exception escaping this function means
    something is wrong with the run as a whole (e.g. scan_store itself
    broken), not with any one ticker - exactly the case scheduler_
    engine.py's retry-cap wants to see as a real failure. Returns
    (captured_count, rejection_counts dict) and persists both a
    quote_snapshot_runs summary row and, per accepted ticker, a
    quote_snapshots row.

    Batch optimisation (Sep 2026): fetches quotes for every ticker up
    front via _fetch_quotes_bulk()/_fetch_quotes_individually() (see
    those functions' own docstrings, and this module's own CALL COST
    section) instead of one yf.Ticker(t).info call per ticker inside
    this loop - the per-ticker QUALITY GATES themselves
    (_classify_market_state/_classify/_is_frozen) and the storage call
    (quote_snapshot_store.record_snapshot) are untouched, called from
    the same one place (_process_one() below) on both the first pass
    and the one retry pass, exactly as they were called inline here
    before this commit.

    Tiered roster (Commit 4, Sep 2026): samples today_roster_tickers(
    market) - the market's own daily tier (research/held tickers plus
    any recently-wide ticker) union today's rotating 1/5th slice of
    the weekly tier - rather than every scanned-universe ticker every
    single day. See today_roster_tickers()/daily_tier_tickers()/
    today_weekly_slice() above for the full tiering logic."""
    tickers = sorted(today_roster_tickers(market))
    now_utc = datetime.now(timezone.utc)
    local_date = now_utc.astimezone(_MARKET_TZ[market]).strftime("%Y-%m-%d")
    currency_fallback = _MARKET_CURRENCY_FALLBACK[market]

    captured = 0
    counts = {
        REJECT_MISSING: 0, REJECT_ASK_LE_BID: 0, REJECT_WIDE_SPREAD: 0,
        REJECT_PRICE_OFF_MID: 0, REJECT_MARKET_NOT_REGULAR: 0,
        REJECT_NO_VOLUME: 0, REJECT_FROZEN_QUOTE: 0,
    }

    def _process_one(ticker, info):
        """One ticker through the SAME quality gates this loop always
        used, unchanged - only `info`'s SOURCE differs from before this
        commit (now a light bulk/individual quote dict, not a
        Ticker.info dict.) Returns the rejection reason, or None once
        the quote is stored."""
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
        if reason is None:
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
                source="yfinance.quote_bulk",
            )
        return reason

    log(f"[quote_recorder] {market}: sampling {len(tickers)} ticker(s) for {local_date}")

    quotes, missing = _fetch_quotes_bulk(tickers, log=log)
    if missing:
        log(f"[quote_recorder] {market}: {len(missing)} ticker(s) not covered by the "
            f"bulk quote endpoint, fetching individually")
        quotes.update(_fetch_quotes_individually(missing, log=log))
    _covered = len(tickers) - len(missing)
    log(f"[quote_recorder] {market}: {_covered} ticker(s) covered by bulk requests, "
        f"{len(missing)} via individual fallback")

    pending_retry = []  # [(ticker, first_pass_reason), ...]
    for ticker in tickers:
        try:
            info = quotes.get(ticker)
            reason = _process_one(ticker, info) if info is not None else REJECT_MISSING
        except Exception as e:
            log(f"[quote_recorder] {market}/{ticker}: failed, skipped ({e})")
            continue
        if reason is None:
            captured += 1
        else:
            pending_retry.append((ticker, reason))

    # One retry pass, later in the SAME run, for every ticker a
    # quality gate rejected on the first pass - conditions inside the
    # sampling window can genuinely shift (a market that had just
    # opened, a momentarily frozen quote). Never for a ticker the
    # first pass simply couldn't fetch/process at all (a real
    # exception - already logged and skipped above, no second guess).
    if pending_retry:
        retry_tickers = [t for t, _r in pending_retry]
        log(f"[quote_recorder] {market}: retrying {len(retry_tickers)} gate-rejected ticker(s)")
        time.sleep(_BULK_REQUEST_PAUSE)
        retry_quotes, retry_missing = _fetch_quotes_bulk(retry_tickers, log=log)
        if retry_missing:
            retry_quotes.update(_fetch_quotes_individually(retry_missing, log=log))
        for ticker, first_reason in pending_retry:
            try:
                info = retry_quotes.get(ticker)
                reason = _process_one(ticker, info) if info is not None else first_reason
            except Exception as e:
                log(f"[quote_recorder] {market}/{ticker}: retry failed, skipped ({e})")
                reason = first_reason
            if reason is None:
                captured += 1
            else:
                counts[reason] += 1

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
