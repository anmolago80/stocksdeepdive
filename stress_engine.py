"""
stress_engine.py

Next-batch instruction, Part 3 ("Stress Test" tab): current holdings +
weights replayed against REAL historical prices/distributions, named
crisis/rally windows, a beta-weighted shock grid, and a historical Monte
Carlo band. Pandas/numpy only - no new dependency.

Why this module needs its OWN price-history fetch, separate from the
site's existing one:

  Every other portfolio tab (and etf_insights.py, Part 2) deliberately
  reuses portfolio_health_engine.fetch_snapshot()'s already-fetched 2y
  history to add zero new per-holding network calls. The Stress Test
  tab's crisis windows reach back to the GFC (Oct 2007) - a 2y window
  cannot possibly cover that, so this module fetches its own longer
  history (yfinance's period="max") per ticker, but - matching the same
  "no new per-pageview network fetch" spirit - caches it on the volume
  (the same shared stocksdeepdive.db every other store in this app
  uses) for 24 hours, so it is fetched at most once per ticker per day
  regardless of how many visitors load the tab, not once per pageview.

Two kinds of history flow through this module:
  - a per-holding OWN price history (get_long_history), used for the
    synthetic full replay and for any scenario window the holding
    actually has data for;
  - a per-holding home INDEX history (^AXJO for a .AX ticker, ^GSPC
    otherwise - see etf_insights.benchmark_ticker_for, reused here
    rather than re-implemented) used to measure that holding's beta and
    to proxy a scenario return when the holding itself has no data for
    that window (flagged (diamond) estimated throughout, per the spec).

IMPORTANT - same environment constraint as etf_insights.py (Part 2):
this sandbox and the device bridge both have zero live path to Yahoo
Finance (every yfinance call fails with a proxied CONNECT error,
confirmed by direct curl test), so this module was written against
yfinance's documented/verified-from-source `history()` shape and
unit-tested against hand-built fixtures spanning many years (including
synthetic drawdowns/rallies), never against Andrew's real holdings'
actual multi-decade history. See the Part 3 report for what to
spot-check after this deploys.

Every function here takes already-fetched data (a DataFrame, a dict of
DataFrames, or plain numbers) and returns plain floats/dicts - never
raises, and returns None/"-"-friendly values on insufficient data
rather than crashing a render.

MEGA-BATCH PART 14 (urgent data fix, 2026-09-06): the owner found
IVV.AX showing an impossible worst drawdown of -93.9% and beta 0.43.
Root cause - get_long_history() fetched yfinance's raw, NOT split-
adjusted, Close series (no explicit auto_adjust argument was ever
passed, leaving it to whatever the installed yfinance version's
default happened to be). iShares split IVV.AX 15-for-1 in Dec 2022;
an unadjusted Close series shows that as a fake ~-93% single-day
crash (1 - 1/15), which then poisoned every downstream number built
from it: max drawdown, best-12m, the monthly-return covariance (hence
every beta), the portfolio headline figures, and the Monte Carlo
inputs. CSL.AX's implausible -68% 15y drawdown was the same fault.

Fix, two layers:
  1. get_long_history() now passes auto_adjust=True EXPLICITLY (never
     relying on the library default again). yfinance's auto_adjust
     replaces Close with Yahoo's own split-and-dividend-back-adjusted
     "Adj Close" - so day-over-day pct-change on Close is now already
     a full TOTAL-RETURN figure (price return + reinvested
     distributions baked in via the adjustment ratio), not a bare
     price return. The separate "Dividends" column yfinance returns
     is the RAW per-share amount on its ex-date - untouched by
     auto_adjust - so daily_returns()/_window_return_from_hist() no
     longer add it on top of the price change (that would now double-
     count every distribution); it's kept in the cache purely because
     etf_insights.ttm_distribution_yield still needs the raw per-share
     figure for its (correctly RAW, not total-return) yield calc.
  2. sanity_checked_history() below is the second line of defence:
     Yahoo's own back-adjustment isn't guaranteed complete for every
     ASX-listed instrument's corporate-action history, so after the
     auto_adjust fetch every holding's OWN price history is still
     checked for a leftover split-sized single-day move; if found, a
     direct one-off yf.Ticker(ticker).splits lookup is used to repair
     it by hand, and if that still doesn't clear the anomaly the
     ticker's history is treated as unusable and excluded from every
     replay that would otherwise be built from it - see that
     function's own docstring.

The whole stress_long_history/stress_result_cache tables were also
renamed (both now end in _v2 - see DB_PATH/_conn() below) rather than
patched in place: every previously-cached price series and assembled
result was potentially built from the old, unadjusted convention, and
a plain schema/table-version bump is the simplest way to guarantee
nothing stale is ever read again, without needing a one-off migration
script that has to run exactly once in production.

FOLLOW-UP TO FIX ROUND 10 #3 (owner instruction, corporate-actions
override table, 2026-09-07): Fix round 10 #3 diagnosed - but
deliberately did not act on - IVV.AX and GOLD.AX both having REAL,
confirmed corporate actions that yfinance's own `.splits` feed
apparently doesn't carry for these ASX-listed, internationally-
domiciled ETPs (empty/incomplete, per every check possible from this
sandbox - see that fix's own diagnostic note a little further down).
This follow-up makes the repair for those two SPECIFIC, source-
verified tickers deterministic rather than dependent on that feed ever
showing up: KNOWN_CORPORATE_ACTIONS below, consulted first by
_attempt_split_repair(). One correction to the instruction that
prompted this table, found while verifying its own claim against the
primary ASX announcement (not a secondary tracker) before hardcoding
anything, per the standing "verify before hardcoding" rule: GOLD.AX's
8 Jun 2022 action was a FORWARD 10:1 split (10 new units per 1 old
unit, roughly $230-260 -> $23-26 per unit, explicitly to improve
retail accessibility per Global X's own announcement text), NOT the
reverse "1-for-10 consolidation" the instruction described - the
opposite direction. Using the wrong direction would have applied the
wrong correction (multiplying instead of dividing), making a false
positive WORSE, not better - exactly what this whole mechanism exists
to prevent. See KNOWN_CORPORATE_ACTIONS's own comment for both
tickers' primary sources.

Also new in this follow-up:
  - get_long_history() now fetches with yfinance's own repair=True
    (previously omitted). yfinance's documented price-repair heuristics
    specifically target the "false positive, no real corporate action"
    failure class the instruction asked about for OCL.AX - missed
    splits not present in .splits, and 100x currency mixups ($/cents)
    that a small, less-liquid ASX stock's feed is a known plausible
    candidate for. OCL.AX's own dividend history was checked first (the
    instruction's own leading hypothesis, a large special distribution
    misread as a price gap) and found to REFUTE it - every distribution
    on record is $0.015-$0.21/share against a multi-dollar unit price,
    nowhere near the guard's 60% threshold (see this fix's own commit
    message / report for the sources). repair=True is the best general-
    purpose fix available without live yfinance access to pin OCL.AX's
    exact offending date by hand (this sandbox has none - see the
    module's own environment-constraint note above); if it doesn't
    fully clear OCL.AX on Railway, the new diagnostic logging just below
    will show exactly which date/move to add as a third
    KNOWN_CORPORATE_ACTIONS entry in a fast follow-up.
  - sanity_checked_history() now also returns a diagnostic (offending
    date + move size) whenever a ticker ends up excluded, so a false
    positive like OCL.AX's is never just a silent "data fault" badge
    again - it's logged server-side and surfaced to admin (see app.py's
    _stress_apply_guard).
  - _v2 -> _v3 table rename (same "guarantee nothing stale is read
    again" reasoning as the _v1 -> _v2 rename above) - a cached history
    or result from before this fix could itself still be the
    unrepaired/excluded series, and this module has no live path to
    reach the production volume from this sandbox to invalidate three
    specific ticker rows by hand, so the same zero-manual-steps
    version-bump pattern is reused rather than a script that would need
    someone to actually run it. This does mean every ticker's cache
    refreshes once, not just IVV.AX/GOLD.AX/OCL.AX's - a one-time,
    cheap, already-proven trade-off, not a targeted invalidation of
    only the three affected tickers as the instruction literally asked
    for; flagged in this fix's own report as a deliberate substitution,
    not an oversight.
"""

import hashlib
import json
import logging
import os
import sqlite3
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import yfinance as yf

import etf_insights

_stress_logger = logging.getLogger("sdd.stress")

# -----------------------------------------------------------------
# Storage - same volume-resolution rule as every other store in this
# app (see etf_insights.py / explain_cache_store.py).
# -----------------------------------------------------------------

def _data_dir():
    return os.environ.get("RAILWAY_VOLUME_MOUNT_PATH") or os.path.dirname(__file__)


DB_PATH = os.path.join(_data_dir(), "stocksdeepdive.db")

LONG_HISTORY_TTL_HOURS = 24  # see module docstring - "nightly-ish"
RESULT_TTL_HOURS = 24        # the assembled stress-test result itself

# Part 26: floor on how far back get_long_history() fetches - see that
# function's own Part 26 note (OCL.AX's IPO-era data-quality problem).
# Years ahead of the earliest CRISES/RALLIES window (GFC, 2007-10-01).
GET_LONG_HISTORY_FLOOR = "2005-01-01"


_LONG_HISTORY_TABLE = "stress_long_history_v4"
_RESULT_CACHE_TABLE = "stress_result_cache_v7"


def _conn():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    # Part 14: _v2 table names (was stress_long_history/stress_result_cache)
    # - see module docstring's "two layers" note. Corporate-actions follow-up
    # (2026-09-07): bumped again to _v3 - same reasoning, see the module
    # docstring's own follow-up paragraph. Every old-versioned table is left
    # in place untouched (harmless, tiny) rather than dropped; it is simply
    # never read again, which is all "invalidate the cache" needs to mean
    # here - every ticker/result recomputes fresh under the current
    # auto_adjust=True + repair=True + sanity-guard convention on its next
    # request.
    #
    # Part 24 (2026-09-08): _RESULT_CACHE_TABLE alone bumped again, to _v4.
    # The very first Stress Test load for a given portfolio's holdings-hash
    # would have run under the repair=True ModuleNotFoundError bug (every
    # get_long_history() call failing, cached as an all-"not enough data"
    # result for RESULT_TTL_HOURS=24) - get_cached_result() has no way to
    # know the underlying fetch is now fixed, so it would keep serving that
    # stale empty result for up to a day even after this exact deploy
    # corrected the fetch. Only the RESULT table needs the bump: nothing
    # bad was ever written to _LONG_HISTORY_TABLE (its own cache_set is
    # only ever called on a non-empty fetch, and every entry already
    # round-trips through plain "YYYY-MM-DD" strings - see _hist_to_json/
    # _hist_from_json - so it was never tz-aware on the way back out
    # either, unaffected by this same part's tz_localize(None) fix above).
    #
    # Part 26 (2026-09-08): both tables bumped again (_v3->_v4, _v4->_v5).
    # Owner-reported: GOLD.AX/IVV.AX/OCL.AX still excluded, and the
    # headline Max Downside/Max Upside charts still suppressed, even
    # after Part 24/25 fixed the rest of the portfolio - see
    # get_long_history()'s and _attempt_split_repair()'s own Part 26
    # notes for what changed. _LONG_HISTORY_TABLE needs the bump this
    # time because get_long_history() now truncates the fetched range
    # (a pre-2005 floor - see that function's own note); an entry cached
    # under the old, untruncated convention would keep serving OCL.AX's
    # IPO-era noise for up to 24h otherwise. _RESULT_CACHE_TABLE needs
    # it for the usual reason (a pre-fix "excluded" result for this
    # portfolio's holdings-hash would otherwise keep being served stale).
    #
    # Part 26 THIRD follow-up (2026-09-08): _RESULT_CACHE_TABLE alone
    # bumped again, _v5->_v6. The prior deploy (this same Part 26's
    # bad-data-window repair) genuinely cleared OCL.AX server-side -
    # confirmed directly from Railway's own logs, no more "stress guard
    # excluded OCL.AX" lines after that deploy - but the owner's own
    # screenshot straight after still showed OCL.AX marked excluded on
    # the page. That deploy didn't bump this table, so the RESULT cached
    # from the load right after the PRIOR deploy (which still excluded
    # OCL.AX) kept being served, unchanged, for up to RESULT_TTL_HOURS -
    # the exact same "cache outlives the fix" mistake this table's own
    # Part 24 and Part 26 bumps above already exist to prevent, repeated
    # by forgetting to apply it to this round too. Every Stress Test
    # result recomputes fresh from here.
    #
    # Part 26 FOURTH follow-up (2026-09-08): bumped again, _v6->_v7, so
    # the new per-candidate diagnostic logging in _attempt_split_repair
    # (added this same round, to see why IVV.AX's real full history
    # scores every repair candidate worse than doing nothing, when a
    # local test using the exact values Railway's own logs showed
    # clears cleanly) actually runs on the next page load instead of a
    # cached "still excluded" verdict serving from before this deploy.
    conn.execute(
        f"""CREATE TABLE IF NOT EXISTS {_LONG_HISTORY_TABLE} (
            ticker TEXT PRIMARY KEY,
            data_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        )"""
    )
    conn.execute(
        f"""CREATE TABLE IF NOT EXISTS {_RESULT_CACHE_TABLE} (
            cache_key TEXT PRIMARY KEY,
            data_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        )"""
    )
    return conn


def _hist_to_json(hist):
    """A Close+Dividends DataFrame -> a compact JSON-able dict. Only the
    two columns the whole module needs are kept (no OHLV/splits) to keep
    the cached blob small."""
    return {
        "dates": [d.strftime("%Y-%m-%d") for d in hist.index],
        "close": [None if pd.isna(v) else float(v) for v in hist["Close"]],
        "dividends": ([None if pd.isna(v) else float(v) for v in hist["Dividends"]]
                      if "Dividends" in hist.columns else [0.0] * len(hist)),
    }


def _hist_from_json(d):
    idx = pd.to_datetime(d["dates"])
    return pd.DataFrame({"Close": d["close"], "Dividends": d["dividends"]}, index=idx)


def _cache_get_history(ticker):
    with _conn() as conn:
        row = conn.execute(
            f"SELECT data_json, created_at FROM {_LONG_HISTORY_TABLE} WHERE ticker = ?",
            (ticker,),
        ).fetchone()
    if row is None:
        return None
    created_at = datetime.fromisoformat(row[1])
    if datetime.now(timezone.utc) - created_at > timedelta(hours=LONG_HISTORY_TTL_HOURS):
        return None
    try:
        return _hist_from_json(json.loads(row[0]))
    except Exception:
        return None


def _cache_set_history(ticker, hist):
    with _conn() as conn:
        conn.execute(
            f"INSERT OR REPLACE INTO {_LONG_HISTORY_TABLE} (ticker, data_json, created_at) VALUES (?, ?, ?)",
            (ticker, json.dumps(_hist_to_json(hist)), datetime.now(timezone.utc).isoformat()),
        )


def get_long_history(ticker, force_refresh=False):
    """Up to ~max available (yfinance period="max") daily Close+
    Dividends for `ticker`, cached 24h on the volume - see module
    docstring for why this is a separate fetch from the site's 2y
    portfolio snapshot. Never raises; an empty DataFrame means no data
    is available (a delisted ticker, or this environment's own no-
    network-access constraint - see module docstring).

    Part 14: auto_adjust=True is now passed EXPLICITLY (previously
    relied on the yfinance default, which is how an unadjusted-for-
    splits Close series slipped through) - see module docstring's
    "two layers" note. The returned "Close" is Yahoo's own split-and-
    dividend-back-adjusted price; "Dividends" stays the raw per-share
    amount, untouched by that adjustment.

    Part 24 (owner-reported: Stress Test showing "not enough shared
    price history" for EVERY holding, including CSL.AX - a highly
    liquid, decades-listed blue chip that unquestionably has data on
    Yahoo Finance, which rules out a real data-availability gap and
    points at the fetch itself). The follow-up to Fix round 10 #3 added
    repair=True to this exact call two deploys ago; that argument was
    never exercised against a live Yahoo Finance connection from any
    prior dev sandbox (this module's own docstring says so plainly), so
    a library-version incompatibility or a repair-heuristic bug specific
    to the tickers in a given portfolio could silently turn EVERY fetch
    into an exception, landing in the bare `except` below with nothing
    logged - previously invisible by design. Two changes address that
    without weakening the repair behaviour when it does work:
      1. the exception is now logged (ticker + repr(exc)) via this
         module's own _stress_logger, so a repeat of exactly this
         failure mode is never silent again;
      2. a single retry WITHOUT repair=True follows a failed repair=True
         attempt - auto_adjust=True alone is the pre-repair, Part 14
         configuration this module already ran on successfully (that
         part's own fix is what caught and corrected the IVV.AX/CSL.AX
         split-adjustment bug in the first place), so this fallback
         costs nothing when repair=True was never the problem and
         restores a real (if less corporate-action-hardened) history
         when it was.

    Part 26 (owner-reported, same complaint as _attempt_split_repair's
    own Part 26 note: GOLD.AX/IVV.AX/OCL.AX still excluded after Part
    24/25). OCL.AX's guard-tripping anomaly (an 85% single-day move) is
    dated 2000-08-17 - within weeks of Objective Corporation's own
    stated ASX listing date (per objective.com's investor page, OCL has
    been "listed since 2000" with FY25 marking "25 years since
    listing"). A brand-new, thinly-traded microcap's first weeks of
    trading are exactly where historical price archives are noisiest
    (sparse quotes, wide spreads, low-volume price discovery) - there is
    no known corporate action to explain this move, and none of this
    period is inside any CRISES/RALLIES window this module actually
    replays (the earliest, the GFC, starts 2007-10-01). Rather than
    trying to "repair" data that most likely was never a clean split or
    dividend event in the first place, GET_LONG_HISTORY_FLOOR simply
    drops everything before it - a safety margin years ahead of the GFC
    window, so nothing this module's own scenario replays or beta/vol
    windows use is ever at risk of being cut short by this."""
    ticker = (ticker or "").strip().upper()
    if not ticker:
        return pd.DataFrame()
    if not force_refresh:
        cached = _cache_get_history(ticker)
        if cached is not None:
            return cached

    def _fetch(**kwargs):
        h = yf.Ticker(ticker).history(period="max", auto_adjust=True, **kwargs)
        if h is None or h.empty:
            return pd.DataFrame()
        h = h[["Close", "Dividends"]] if "Dividends" in h.columns else h[["Close"]].assign(Dividends=0.0)
        # Part 24 (2nd finding, same owner report): a fresh yfinance fetch
        # returns a timezone-AWARE DatetimeIndex (e.g. "Australia/Sydney"
        # for an ASX-listed stock), but this module also fetches home-
        # index tickers (^AXJO/^GSPC) the same way - and Yahoo does not
        # guarantee the same tz convention for an index ticker as for an
        # individual stock. Reproduced directly (no live network needed):
        # pd.concat([...], axis=1, join="inner") on two Series whose
        # DatetimeIndexes differ only in tz-awareness silently returns ZERO
        # rows (no exception) - which is exactly compute_beta()'s alignment
        # step, so a tz mismatch between a holding and its benchmark index
        # would silently zero out every beta, hence portfolio_beta() and
        # the whole Shock Grid, with nothing to show for why. Stripping
        # the tz here (to plain, naive timestamps) makes every fetch's
        # index consistent regardless of what Yahoo happened to tag it
        # with, and also matches what a CACHED fetch already looks like
        # (_hist_from_json rebuilds the index from plain "YYYY-MM-DD"
        # strings, which is always tz-naive) - so a first-time fetch and a
        # cached one behave identically instead of only one of them being
        # safe to compare against another ticker.
        if h.index.tz is not None:
            h.index = h.index.tz_localize(None)
        # Part 26: drop IPO-era/pre-history noise below the floor - see
        # this function's own Part 26 docstring note (OCL.AX).
        h = h[h.index >= pd.Timestamp(GET_LONG_HISTORY_FLOOR)]
        return h

    hist = pd.DataFrame()
    try:
        hist = _fetch(repair=True)
    except Exception as exc:
        _stress_logger.warning("get_long_history(%s): repair=True fetch failed (%r); retrying without repair", ticker, exc)
        try:
            hist = _fetch(repair=False)
        except Exception as exc2:
            _stress_logger.warning("get_long_history(%s): retry without repair also failed (%r)", ticker, exc2)
            hist = pd.DataFrame()
    try:
        if not hist.empty:
            _cache_set_history(ticker, hist)
    except Exception:
        pass
    return hist


# -----------------------------------------------------------------
# Named scenario windows - per the instruction's own list.
# -----------------------------------------------------------------

CRISES = [
    ("gfc", "GFC", "2007-10-01", "2009-03-31"),
    ("q4_2018", "2018 Q4", "2018-10-01", "2018-12-31"),
    ("covid", "COVID crash", "2020-02-01", "2020-03-31"),
    ("rate_shock_2022", "2022 rate shock", "2022-01-01", "2022-10-31"),
]

RALLIES = [
    ("post_gfc", "Post-GFC rebound", "2009-03-01", "2009-12-31"),
    ("melt_up_2016_17", "2016-17 melt-up", "2016-01-01", "2017-12-31"),
    ("covid_recovery", "COVID recovery", "2020-04-01", "2021-03-31"),
    ("ai_rally_2023_24", "2023-24 AI rally", "2023-01-01", "2024-12-31"),
]


def home_index_for(ticker):
    """ASX 200 for a .AX ticker, S&P 500 otherwise - reuses Part 2's
    own convention rather than re-deriving it."""
    return etf_insights.benchmark_ticker_for(ticker)


# -----------------------------------------------------------------
# Part 14 sanity guard: a split-sized single-day move that survived
# get_long_history()'s auto_adjust=True fetch (Yahoo's own back-
# adjustment isn't guaranteed complete for every ASX-listed
# instrument's corporate-action history - the IVV.AX finding that
# prompted this whole part). A fund/ETF tracking a diversified index
# essentially never gaps this hard even in a real crash, so it gets a
# tighter bar than a single stock, which can legitimately gap on
# binary news (a takeover, a trial result, a delisting).
# -----------------------------------------------------------------

SPLIT_ANOMALY_THRESHOLD_FUND = 0.40
SPLIT_ANOMALY_THRESHOLD_STOCK = 0.60

# Fix round 10 #3 diagnostic note (owner-reported: IVV.AX/OCL.AX/GOLD.AX
# all showing "data fault - excluded" in Stress Test). This sandbox has
# no live yfinance access, so the root cause below is evidenced from
# public corporate-action records cross-checked against ASX announcements
# and third-party split trackers, NOT from calling yf.Ticker(...).splits
# directly - that live check still needs doing on Railway, where the app
# actually runs.
#   - IVV.AX genuinely split ~15:1 in Dec 2022, and GOLD.AX genuinely did
#     a FORWARD 10:1 split on 8 Jun 2022 (ASX announcement
#     459h6dtw1n4ncb.pdf - 9 new units issued per 1 held, ~$230-260 ->
#     ~$23-26/unit; CORRECTED from this note's original "1-for-10
#     REVERSE split" guess, which had the direction backwards - see the
#     KNOWN_CORPORATE_ACTIONS follow-up note in the module docstring) -
#     so the instruction's own assumption that "OCL/GOLD have no splits
#     to fault" does not hold for GOLD.AX; it has a real, confirmed
#     split. The likely reason _attempt_split_repair
#     still fails for both IVV.AX and GOLD.AX is that yfinance's own
#     `Ticker(...).splits` corporate-actions feed is known to be
#     incomplete for ASX-listed, internationally-domiciled ETPs (it's
#     sourced primarily for US-listed securities) - i.e. the repair logic
#     above is very likely correct, but the split it needs from Yahoo
#     probably never arrives. This needs a live check (log
#     `yf.Ticker("IVV.AX").splits` / `yf.Ticker("GOLD.AX").splits` on
#     Railway) to confirm before changing the repair logic itself.
#   - OCL.AX has no confirmed split anywhere in public records searched -
#     its guard trip looks like a genuine false positive unrelated to any
#     split (a large special dividend/distribution misread as a price
#     gap is the leading hypothesis per the instruction's own framing,
#     but this also needs OCL.AX's actual Close series, live, to confirm
#     the exact date/cause).
# Deliberately NOT changing the repair-matching logic itself on this
# evidence alone (that would mean guessing at which large moves are
# "safe" to silently accept as real, and getting that wrong risks feeding
# genuinely corrupted data into a drawdown/beta/Monte-Carlo computation -
# exactly what this guard exists to prevent). What Fix round 10 #3 does
# add is the "guard on the guard" below _stress_apply_guard's call site
# in app.py: once excluded weight passes ~25% of the portfolio, the
# portfolio-wide headline numbers are suppressed rather than computed
# from a rump of survivors and shown as if authoritative.


def _worst_single_day_move(hist):
    """The single largest-magnitude day-over-day Close move in `hist`,
    as (date, move_pct) - move_pct signed (e.g. -93.9, not 93.9) so the
    caller/log can tell a crash-shaped move from a split-shaped one at a
    glance. (None, None) if `hist` has under 2 usable rows. Split out of
    the old bool-only _detect_anomalous_move so the guard-trip diagnostic
    (offending date + move size, per the corporate-actions follow-up)
    can reuse the same computation instead of re-deriving it."""
    if hist is None or hist.empty or "Close" not in hist.columns or len(hist) < 2:
        return None, None
    close = hist["Close"].astype(float)
    ret = close.pct_change().dropna()
    if ret.empty:
        return None, None
    worst_date = ret.abs().idxmax()
    return worst_date, float(ret.loc[worst_date]) * 100.0


def _detect_anomalous_move(hist, threshold):
    """True if `hist`'s Close has any single-day move beyond
    `threshold` in absolute value - the guard's trigger condition. Thin
    wrapper over _worst_single_day_move (kept for the two existing call
    sites in sanity_checked_history that only need the bool)."""
    _date, move_pct = _worst_single_day_move(hist)
    if move_pct is None:
        return False
    return abs(move_pct) / 100.0 > threshold


KNOWN_CORPORATE_ACTIONS = {
    # ratio follows yfinance's own .splits convention (new units per old
    # unit - 15.0 for a 15:1 forward split, 0.1 for a 1:10 reverse
    # split), so _apply_split_ratio below is the exact same divide-
    # before-the-effective-date arithmetic as the yfinance-fed path in
    # the general case further down - never a second convention to keep
    # in sync.
    "IVV.AX": {
        "ratio": 15.0,
        "effective_date": "2022-12-07",
        "source": (
            "iShares/BlackRock ASX announcement, 23 Nov 2022 "
            "(announcements.asx.com.au/asxpdf/20221123/pdf/45hymhl1q63mqp.pdf): "
            "15-for-1 forward split, 15 new units issued per 1 unit held. "
            "2022-12-07 is when trading in post-split units began "
            "(deferred-settlement code IVVDB); normal settlement under the "
            "original IVV code resumed 2022-12-13 - if Yahoo's feed turns "
            "out to price the gap through settlement resumption instead, "
            "2022-12-13 is the fallback date to try."
        ),
    },
    "GOLD.AX": {
        "ratio": 10.0,
        "effective_date": "2022-06-08",
        "source": (
            "Global X ASX announcement, 31 May 2022 "
            "(announcements.asx.com.au/asxpdf/20220531/pdf/459h6dtw1n4ncb.pdf): "
            "FORWARD 10:1 split (9 new units issued per 1 held, 10 total), "
            "reducing unit price from ~$230-260 to ~$23-26 for retail "
            "accessibility. 2022-06-08 is the announcement's own 'trading "
            "commences in post-split units' date. NOTE: the owner instruction "
            "that prompted this table described this as a '1-for-10 "
            "consolidation' (i.e. a REVERSE split) - verified against this "
            "primary ASX source before hardcoding (per that same instruction's "
            "own 'verify before hardcoding' rule) and found to be the OPPOSITE "
            "direction. Hardcoding the stated reverse direction would have "
            "multiplied the pre-split price instead of dividing it, turning a "
            "false positive into a wrong number instead of a fixed one - "
            "flagged in this fix's own commit/report rather than silently "
            "corrected with no trace."
        ),
    },
}


def _apply_split_ratio(hist, effective_date, ratio):
    """Retroactively divides every Close price strictly before
    `effective_date` by `ratio` (yfinance .splits convention - see
    KNOWN_CORPORATE_ACTIONS's own comment). Shared by both the known-
    table path and the general yfinance-.splits path below so the two
    never drift into different arithmetic. Returns a repaired copy;
    `hist` itself is never mutated."""
    repaired = hist.copy()
    idx = repaired.index
    if getattr(idx, "tz", None) is not None:
        idx = idx.tz_localize(None)
        repaired.index = idx
    ed = pd.Timestamp(effective_date)
    if ed.tzinfo is not None:
        ed = ed.tz_localize(None)
    before = repaired.index < ed
    repaired.loc[before, "Close"] = repaired.loc[before, "Close"] / ratio
    return repaired


KNOWN_ACTION_DATE_TOLERANCE_DAYS = 14  # see _attempt_split_repair's Part 26 note
SPIKE_REVERSION_TOLERANCE = 0.20  # see _repair_isolated_spike's own docstring
SPIKE_REVERSION_WINDOW_DAYS = 10  # how many trading days ahead to look for a reversion


def _repair_isolated_spike(hist):
    """Detects and repairs a TRANSIENT bad-data window - a run of one or
    more days where Close diverges sharply from its established level
    and later comes back close to it - as distinct from a genuine,
    PERMANENT level shift (a real split, or a real sustained crash/rally
    that holds). This is a fundamentally different failure shape from
    everything _apply_split_ratio/_attempt_split_repair's known-table
    and yfinance-.splits paths handle: those model a step change (every
    day before the boundary shifts the same way, forever after); this
    is V-shaped - the level itself never really changed, only some of
    the prints did.

    Part 26 follow-up (owner: still "no graph" and all three tickers
    still excluded, even after boundary auto-detection and the
    pre-2005 floor cleared each ticker's ORIGINAL flagged anomaly).
    Checking Railway's logs for this exact deploy: each of GOLD.AX/
    IVV.AX/OCL.AX is now failing on a SECOND, previously-hidden
    anomaly, at a date with no matching corporate action anywhere
    (confirmed by directly searching ASX announcement archives - only
    GOLD.AX's known 2022 split exists on record).

    IMPORTANT arithmetic note that shaped this function: a clean,
    single-day bad print that fully reverts on the VERY NEXT day would
    always show the REBOUND (not the drop) as the larger single-day
    move by percentage - dropping 90% and recovering back to par is a
    +900% day, bigger in magnitude than the -90% that caused it. Since
    GOLD.AX's own flagged worst move is the DROP itself (-89.9% on
    2011-01-04, not some huge positive rebound the next day), a same-
    next-day full revert is arithmetically ruled out for it - so this
    function searches a WINDOW of up to SPIKE_REVERSION_WINDOW_DAYS
    trading days ahead for a return to the pre-divergence level, not
    just the immediate next day, and repairs the WHOLE bad span (not
    just one row) via straight-line interpolation between the last good
    day and the first day that's back to normal - covering both a
    single bad tick and a short run of bad prints that only self-
    correct some days later, whichever this turns out to be once
    deployed.

    If no later day within the window comes back close to the pre-
    divergence level, this function leaves `hist` completely untouched
    - that's either a genuine, persisting level shift, or a real,
    extreme market move (e.g. OCL.AX's 61.8% move on 2008-10-14 sits
    squarely inside the real Oct 2008 GFC crash week and may simply be
    real crisis-era volatility for a small-cap, not a data fault at
    all) - and no version of this codebase should silently overwrite
    that.

    Returns hist unchanged (never raises) if there isn't a real prior
    day to anchor on, or no reversion is found within the window."""
    worst_date, _worst_move = _worst_single_day_move(hist)
    if worst_date is None or hist is None or "Close" not in hist.columns:
        return hist
    idx = hist.index
    try:
        pos = idx.get_loc(worst_date)
    except KeyError:
        return hist
    if not isinstance(pos, (int, np.integer)) or pos < 1:
        return hist
    close = hist["Close"].astype(float)
    anchor_before = close.iloc[pos - 1]
    if anchor_before <= 0:
        return hist

    reversion_pos = None
    limit = min(pos + SPIKE_REVERSION_WINDOW_DAYS, len(idx) - 1)
    for j in range(pos + 1, limit + 1):
        val = close.iloc[j]
        if val > 0 and abs(val - anchor_before) / anchor_before <= SPIKE_REVERSION_TOLERANCE:
            reversion_pos = j
            break
    if reversion_pos is None:
        return hist  # no reversion within the window - leave a real/persisting move alone

    repaired = hist.copy()
    close_col = repaired.columns.get_loc("Close")
    anchor_after = close.iloc[reversion_pos]
    span = reversion_pos - (pos - 1)
    for k, j in enumerate(range(pos, reversion_pos), start=1):
        repaired.iloc[j, close_col] = anchor_before + (anchor_after - anchor_before) * (k / span)
    return repaired


def _attempt_split_repair(ticker, hist):
    """First consults KNOWN_CORPORATE_ACTIONS (see module docstring's
    follow-up note): for a ticker with an entry there, the fix is
    applied from that source-verified, hardcoded ratio ONLY -
    yfinance's own `.splits` feed is not consulted at all for that
    ticker, both because it's already known to be empty/unreliable for
    these specific listings (see the Fix round 10 #3 diagnostic note
    above) and to avoid ever double-applying a correction from two
    sources at once.

    For every other ticker, falls back to the original general-case
    path: `ticker`'s own split-event log is fetched directly (a small,
    one-off, uncached call - only ever reached once
    _detect_anomalous_move has already found a problem, so this never
    runs on the healthy path) and every split it lists is applied via
    _apply_split_ratio - exactly what auto_adjust=True (and, now,
    repair=True) are already supposed to have done, redone by hand as a
    second line of defence for whichever listings Yahoo's own back-
    adjustment doesn't fully cover.

    Part 26 (owner-reported: GOLD.AX/IVV.AX still excluded after Part
    24/25, despite GOLD.AX having a sourced, matching-date
    KNOWN_CORPORATE_ACTIONS entry). Production's own diagnostic gave
    the answer: GOLD.AX's post-repair worst move landed exactly ON its
    hardcoded effective_date, POSITIVE and even larger than the
    original unrepaired move (905.0%, vs. the clean ~-90% a real
    unrepaired 10:1 split should show) - the signature of a divide
    boundary planted one or more days short of where Yahoo's raw feed
    actually prices the jump (the IVV.AX entry's own source note
    already anticipated exactly this risk: "if Yahoo's feed turns out
    to price the gap through settlement resumption instead, 2022-12-13
    is the fallback date to try" - deferred-settlement trading codes can
    genuinely shift which calendar day a data vendor attributes the
    move to). Two changes:

      1. Boundary candidates. `hist`'s OWN worst-move date (computed
         fresh here, before any repair) is where the raw data actually
         shows its biggest jump - by construction, that's a real
         boundary as Yahoo represents it, whatever day that is. Both
         that auto-detected date AND the sourced effective_date are
         tried as separate candidates (same sourced, verified ratio
         either way - only the divide-from day differs) rather than
         picking one - see the Part 26 second-follow-up note below for
         why the auto-detected date can legitimately land nowhere near
         the sourced one and still be exactly right.
      2. Self-correction (every path, every candidate): a "repaired"
         series is only used if its own worst move is genuinely smaller
         in magnitude than the original's, and only the single best
         candidate overall is ever returned. This can't fix a ticker
         that's actually faulty, but it guarantees a repair attempt is
         never itself the reason a usable series ends up excluded - the
         exact failure mode production hit, and it's also what makes
         trying multiple boundary candidates safe: an unrelated
         anomaly's date won't accidentally get the known ratio applied
         to it, because doing so would only make that unrelated
         anomaly's own worst move bigger, not smaller, and lose out to
         "leave it alone" every time.

    Part 26 follow-up: now also tries _repair_isolated_spike (a bad-tick
    fix, a different failure shape entirely from a split - see that
    function's own docstring) as another candidate, and keeps whichever
    candidate's own worst move is smallest (still only if that beats
    the original).

    Part 26 SECOND follow-up (owner: "same thing nothing chaged" after
    the first Part 26 deploy). Checked Railway's own logs again - this
    time with the new Close-window diagnostic logging from that same
    deploy, giving real data instead of another guess. GOLD.AX's window
    around its now-worst date (2011-01-04): ~135 for the prior two
    weeks, then a clean, PERMANENT step down to ~13.5 that holds for the
    following two weeks - ratio 135/13.5 = 9.997, i.e. almost exactly
    10.0, GOLD.AX's own KNOWN_CORPORATE_ACTIONS ratio. IVV.AX's window
    around ITS now-worst date (also 2011-01-04): ~100 for two weeks,
    then a permanent step to ~6.8 - ratio 100/6.8 = 14.7, close to
    IVV.AX's own known ratio of 15.0. Both tickers stepping down by
    almost exactly their OWN real future 2022 split ratio, on the exact
    same calendar date, is not a coincidence and not a second real
    corporate action - it's a Yahoo/vendor data-processing seam: the
    correct eventual ratio has apparently been retroactively baked into
    this feed's numbers from 2011-01-04 onward, over a decade before the
    2022 split actually happened, while data before that internal
    cutoff was left at the original, unadjusted scale. KNOWN_ACTION_
    DATE_TOLERANCE_DAYS (14 days) was built around a much smaller
    settlement-timing slip and would never have caught a boundary this
    far from the sourced date - so it's removed; both the sourced date
    and the auto-detected date are now tried unconditionally as
    candidates, letting self-correction alone (see point 2 above) decide
    per ticker which one, if either, actually helps.

    Returns a (possibly) repaired copy of `hist`; never raises - a
    failed lookup, an empty split log, or a repair that doesn't
    actually help just returns `hist` unchanged, and the caller's own
    re-check after this call is what actually decides whether the
    ticker ends up usable."""
    orig_date, orig_move = _worst_single_day_move(hist)
    candidates = []  # [(repaired_hist, abs(worst_move)), ...]

    spike_repaired = _repair_isolated_spike(hist)
    if spike_repaired is not hist:
        _, spike_move = _worst_single_day_move(spike_repaired)
        if spike_move is not None:
            candidates.append((spike_repaired, abs(spike_move)))

    known = KNOWN_CORPORATE_ACTIONS.get((ticker or "").strip().upper())
    if known is not None:
        boundaries = {known["effective_date"]}
        if orig_date is not None:
            od = orig_date
            if getattr(od, "tzinfo", None) is not None:
                od = od.tz_localize(None)
            boundaries.add(od)
        for boundary in boundaries:
            try:
                cand = _apply_split_ratio(hist, boundary, known["ratio"])
            except Exception:
                continue
            _, cand_move = _worst_single_day_move(cand)
            if cand_move is not None:
                candidates.append((cand, abs(cand_move)))
    else:
        try:
            splits = yf.Ticker(ticker).splits
        except Exception:
            splits = None
        if splits is not None and len(splits) > 0:
            split_repaired = hist
            for split_date, ratio in splits.items():
                try:
                    ratio = float(ratio)
                except (TypeError, ValueError):
                    continue
                if not ratio or ratio == 1.0:
                    continue
                try:
                    split_repaired = _apply_split_ratio(split_repaired, split_date, ratio)
                except Exception:
                    continue
            if split_repaired is not hist:
                _, split_move = _worst_single_day_move(split_repaired)
                if split_move is not None:
                    candidates.append((split_repaired, abs(split_move)))

    if not candidates or orig_move is None:
        _stress_logger.warning(
            "_attempt_split_repair(%s): no candidates produced (orig_move=%s)",
            ticker, orig_move,
        )
        return hist
    best_hist, best_abs_move = min(candidates, key=lambda c: c[1])
    # Part 26 THIRD follow-up diagnostic (owner: IVV.AX still excluded after
    # a fix that reproduces clean in a local, synthetic test using the exact
    # values Railway's own log showed - meaning something in IVV.AX's REAL,
    # FULL history outside that narrow logged window is making every
    # candidate here score worse than just leaving hist alone, and this is
    # the only way to see which candidate and why without guessing again.
    best_worst_date, _ = _worst_single_day_move(best_hist)
    _stress_logger.warning(
        "_attempt_split_repair(%s): %d candidate(s) tried, orig_move=%.1f%%, "
        "best_candidate_abs_move=%.1f%% (its own worst day now %s) - %s",
        ticker, len(candidates), abs(orig_move), best_abs_move, best_worst_date,
        "using best candidate" if best_abs_move < abs(orig_move) else "keeping original, no candidate helped",
    )
    if best_abs_move < abs(orig_move):
        return best_hist
    return hist


def sanity_checked_history(ticker, hist, is_fund):
    """Part 14's sanity guard. `hist` (already fetched with
    auto_adjust=True, repair=True) is checked for a single-day move
    beyond SPLIT_ANOMALY_THRESHOLD_FUND (funds/ETFs) or _STOCK
    (everything else). A clean series is returned unchanged. An
    anomalous one goes through _attempt_split_repair (KNOWN_CORPORATE_
    ACTIONS first, yfinance .splits as the general-case fallback - see
    that function's own docstring) and is re-checked; if that clears
    it, the repaired series is returned; if it doesn't, this ticker's
    history is treated as unusable - excluded, per the same reasoning
    as before, rather than ever feeding a corrupted series into a
    drawdown/beta/Monte-Carlo computation.

    Corporate-actions follow-up (2026-09-07): now returns a 3-tuple,
    (checked_hist_or_None, was_faulty, diagnostic_or_None). diagnostic
    is populated whenever the ticker ends up excluded - {"date",
    "move_pct", "threshold_pct"} for the offending day found on the
    LAST check performed (i.e. post-repair if a repair was attempted -
    the day still tripping the guard after every available fix, which
    is the one worth surfacing) - and a warning is logged server-side
    at the same time, so a false positive like OCL.AX's is never just a
    silent "data fault" badge again."""
    threshold = SPLIT_ANOMALY_THRESHOLD_FUND if is_fund else SPLIT_ANOMALY_THRESHOLD_STOCK
    worst_date, worst_move = _worst_single_day_move(hist)
    if worst_move is None or abs(worst_move) / 100.0 <= threshold:
        return hist, False, None

    repaired = _attempt_split_repair(ticker, hist)
    r_date, r_move = _worst_single_day_move(repaired)
    if r_move is None or abs(r_move) / 100.0 <= threshold:
        return repaired, False, None

    diagnostic = {"date": str(r_date.date()) if hasattr(r_date, "date") else str(r_date),
                  "move_pct": round(r_move, 1), "threshold_pct": round(threshold * 100.0, 1)}
    _stress_logger.warning(
        "stress guard excluded %s: worst single-day move %.1f%% on %s "
        "(threshold %.0f%%, fund=%s) - _attempt_split_repair did not clear it",
        ticker, r_move, diagnostic["date"], threshold * 100.0, is_fund,
    )
    # Part 26 follow-up: every guess so far (boundary auto-detection, the
    # pre-2005 floor, the isolated-spike repair above) has been built on
    # inference from a single (date, move_pct) pair rather than the raw
    # data itself - and the last two rounds of "fixed it" both turned out
    # to only be partially right. Logging the actual Close values in a
    # window around the still-offending date, on the REPAIRED series (so
    # this reflects whatever _attempt_split_repair already tried), means
    # the next Railway check is reading real evidence instead of guessing
    # again from one number.
    try:
        _log_window_around(ticker, repaired if repaired is not None else hist, r_date)
    except Exception:
        pass
    return None, True, diagnostic


def _log_window_around(ticker, hist, center_date, days=8):
    """Part 26 follow-up diagnostic: logs Close[center_date-days ..
    center_date+days] (date: value, one per line via %s) so a guard
    exclusion this codebase couldn't resolve on the first guess leaves
    real, inspectable evidence behind instead of just a single (date,
    move_pct) pair. Read-only, never raises to its caller (wrapped in
    try/except at the call site) - a logging failure must never affect
    which tickers the guard excludes."""
    if hist is None or hist.empty or "Close" not in hist.columns:
        return
    idx = hist.index
    try:
        pos = idx.get_loc(center_date)
    except KeyError:
        return
    if not isinstance(pos, (int, np.integer)):
        return
    lo = max(0, pos - days)
    hi = min(len(idx) - 1, pos + days)
    window = hist["Close"].astype(float).iloc[lo:hi + 1]
    lines = ", ".join(f"{d.date()}={v:.4f}" for d, v in window.items())
    _stress_logger.warning("stress guard %s: Close window around %s -> %s", ticker, center_date, lines)


# -----------------------------------------------------------------
# Returns, beta, drawdown - all from a Close+Dividends DataFrame.
# -----------------------------------------------------------------

def daily_returns(hist):
    """Simple daily total-return series - plain pct-change on `hist`'s
    Close. None if `hist` has under 2 rows.

    Part 14: `hist`'s Close now comes from get_long_history()'s
    auto_adjust=True fetch, which is ALREADY a total-return series
    (Yahoo's own back-adjustment folds reinvested distributions into
    the price) - so no per-share Dividends are added here any more
    (doing so on top of an adjusted Close would double-count every
    distribution). See module docstring."""
    if hist is None or hist.empty or "Close" not in hist.columns or len(hist) < 2:
        return None
    close = hist["Close"].astype(float)
    ret = close.pct_change()
    return ret.dropna()


def _monthly_returns(hist):
    """Delegates to etf_insights' own implementation (same "resample to
    month-end Close" logic) rather than re-implementing it."""
    return etf_insights._monthly_returns(hist)


def compute_beta(holding_hist, index_hist, min_months=12):
    """OLS slope of holding monthly returns on index monthly returns
    (cov/var) - None with fewer than `min_months` overlapping months. A
    lower bar than etf_insights.correlation_vs's 24-month floor (a beta
    for a young holding is still useful directionally; the caller flags
    any downstream figure computed FROM a proxy that itself used this
    beta, per the spec's own (diamond) estimated marker)."""
    a = _monthly_returns(holding_hist)
    b = _monthly_returns(index_hist)
    if a is None or b is None:
        return None
    aligned = pd.concat([a, b], axis=1, join="inner").dropna()
    if len(aligned) < min_months:
        return None
    x = aligned.iloc[:, 1].values
    y = aligned.iloc[:, 0].values
    var_x = np.var(x, ddof=1)
    if var_x == 0:
        return None
    cov_xy = np.cov(x, y, ddof=1)[0, 1]
    return float(cov_xy / var_x)


def build_combined_series(weights, histories):
    """Portfolio total-return index (starts at 1.0) from `weights`
    ({ticker: weight fraction, weights need not sum to 1 - they are
    renormalized here}) and `histories` ({ticker: Close+Dividends
    DataFrame}).

    Combined on the INNER-joined common date range across every
    weighted holding that has any history at all (holdings with no
    history are simply excluded, with their weight renormalized across
    the rest) - so the actual replay window is naturally capped by
    whichever included holding has the SHORTEST history. This is a
    simplification worth stating plainly: a portfolio whose youngest
    holding was bought last year will only replay ~1y of real history
    even if its other holdings go back to the 1990s. Returns
    (series, tickers_used, tickers_dropped)."""
    weights = {t: w for t, w in (weights or {}).items() if w}
    total_w = sum(weights.values())
    if total_w <= 0:
        return None, [], list(weights.keys())

    rets = {}
    dropped = []
    for t, w in weights.items():
        h = (histories or {}).get(t)
        r = daily_returns(h)
        if r is None or r.empty:
            dropped.append(t)
            continue
        rets[t] = r
    if not rets:
        return None, [], dropped

    used_w_total = sum(weights[t] for t in rets)
    if used_w_total <= 0:
        return None, [], list(weights.keys())
    norm_w = {t: weights[t] / used_w_total for t in rets}

    df = pd.concat(rets, axis=1, join="inner")
    if df.empty:
        return None, [], list(weights.keys())
    port_daily_ret = sum(df[t] * norm_w[t] for t in rets)
    series = (1.0 + port_daily_ret).cumprod()
    return series, list(rets.keys()), dropped


def cap_to_years(hist, years):
    """The last `years` years of `hist` (or all of it if shorter) - used
    to bound the "up to 15y" synthetic full-replay window (drawdown/
    best-12m/replayed-return) to what the instruction actually asks
    for, WITHOUT capping the separate scenario-replay/beta history
    (those need the FULL available history - a 15y cap from "today"
    would exclude the GFC entirely)."""
    if hist is None or hist.empty:
        return hist
    cutoff = hist.index[-1] - pd.Timedelta(days=int(365.25 * years))
    return hist[hist.index >= cutoff]


def drawdown_series(series):
    """(value / running all-time-high - 1) at every point - always <= 0.
    The chart-ready mirror of max_drawdown()'s single worst figure."""
    if series is None or series.empty:
        return None
    return series / series.cummax() - 1.0


def runup_from_trough_series(series):
    """(value / running all-time-low - 1) at every point - always >= 0,
    and the deliberate symmetric counterpart to drawdown_series() above
    (that one tracks distance below the running HIGH; this one tracks
    distance above the running LOW). Reads as "how much the portfolio
    has recovered/grown from the worst point seen so far" - it only
    resets toward 0 when an actual new all-time low is set, not on some
    fixed schedule."""
    if series is None or series.empty:
        return None
    return series / series.cummin() - 1.0


def max_drawdown(series):
    """Deepest peak-to-trough decline in `series` (a total-return index)
    - {"pct", "peak_date", "trough_date", "recovered_date",
    "months_to_recover"} (the last two None if the series never
    recovers back to its pre-drawdown peak by the series' last point).
    None if `series` is None/empty."""
    if series is None or series.empty:
        return None
    running_max = series.cummax()
    drawdown = series / running_max - 1.0
    trough_date = drawdown.idxmin()
    trough_val = float(drawdown.loc[trough_date])
    peak_date = series.loc[:trough_date].idxmax()
    recovery_level = float(series.loc[peak_date])
    after_trough = series.loc[trough_date:]
    recovered = after_trough[after_trough >= recovery_level]
    recovered_date = recovered.index[0] if len(recovered) else None
    months_to_recover = None
    if recovered_date is not None:
        months_to_recover = round((recovered_date - trough_date).days / 30.44, 1)
    return {
        "pct": trough_val * 100.0,
        "peak_date": peak_date, "trough_date": trough_date,
        "recovered_date": recovered_date, "months_to_recover": months_to_recover,
    }


def best_rolling_12m(series):
    """Best rolling ~252-trading-day (12-month) return in `series` -
    {"pct", "start_date", "end_date"}, or None if `series` has under a
    year of data."""
    if series is None or len(series) < 253:
        return None
    window = 252
    rolled = series / series.shift(window) - 1.0
    rolled = rolled.dropna()
    if rolled.empty:
        return None
    end_date = rolled.idxmax()
    pct = float(rolled.loc[end_date])
    start_date = series.index[series.index.get_loc(end_date) - window]
    return {"pct": pct * 100.0, "start_date": start_date, "end_date": end_date}


def replayed_return_pa(series, years=10):
    """Annualised return of `series` over its own last `years` years (or
    its full span if shorter) - None if under a year of data."""
    if series is None or len(series) < 2:
        return None
    end_dt = series.index[-1]
    start_dt = end_dt - pd.Timedelta(days=int(365.25 * years))
    window = series[series.index >= start_dt]
    if len(window) < 2:
        return None
    span_days = (window.index[-1] - window.index[0]).days
    if span_days < 365:
        return None
    total_growth = float(window.iloc[-1]) / float(window.iloc[0])
    if total_growth <= 0:
        return None
    actual_years = span_days / 365.25
    return (total_growth ** (1.0 / actual_years) - 1.0) * 100.0


# -----------------------------------------------------------------
# Scenario replays - crisis/rally windows, with sector/index x beta
# proxying for a holding that lacks data over the window.
# -----------------------------------------------------------------

def _window_return_from_hist(hist, start, end):
    """Actual total return of one holding's OWN history within
    [start, end], or None if it has under 2 data points inside the
    window (i.e. it wasn't trading / has no cached history there - the
    caller proxies in that case).

    Part 14: plain price growth on `hist`'s (auto_adjust=True) Close -
    no per-share Dividends added on top; see daily_returns()'s
    docstring for why that would now double-count distributions."""
    if hist is None or hist.empty or "Close" not in hist.columns:
        return None
    window = hist[(hist.index >= start) & (hist.index <= end)]
    if len(window) < 2:
        return None
    start_price = float(window["Close"].iloc[0])
    if start_price <= 0:
        return None
    end_price = float(window["Close"].iloc[-1])
    return end_price / start_price - 1.0


def scenario_replay(weights, histories, index_histories, betas, window, total_value_aud):
    """One named crisis/rally window -> per-holding rows (ticker,
    move_pct, estimated bool) + the portfolio's weighted move (% and
    $). A holding with no own-history coverage of the window is proxied
    as `home_index's actual move over the window x that holding's own
    beta` and flagged estimated=True; a holding with neither its own
    coverage NOR a computable beta is simply excluded (weight
    renormalized across the rest, same convention as
    build_combined_series) rather than guessed at with no basis at all.
    """
    _key, label, start, end = window
    rows = []
    weights = {t: w for t, w in (weights or {}).items() if w}
    for t, w in weights.items():
        hist = (histories or {}).get(t)
        actual = _window_return_from_hist(hist, start, end)
        if actual is not None:
            rows.append({"ticker": t, "weight": w, "move_pct": actual * 100.0, "estimated": False})
            continue
        beta = (betas or {}).get(t)
        idx_ticker = home_index_for(t)
        idx_hist = (index_histories or {}).get(idx_ticker)
        idx_move = _window_return_from_hist(idx_hist, start, end)
        if beta is not None and idx_move is not None:
            rows.append({"ticker": t, "weight": w, "move_pct": beta * idx_move * 100.0, "estimated": True})
        # else: no basis at all for this holding in this window - excluded.

    total_w = sum(r["weight"] for r in rows)
    if total_w <= 0:
        return {"key": _key, "label": label, "start": start, "end": end,
                "move_pct": None, "move_value_aud": None, "rows": rows, "best": None, "worst": None}

    move_pct = sum(r["move_pct"] * r["weight"] for r in rows) / total_w
    move_value_aud = (move_pct / 100.0) * total_value_aud if total_value_aud else None
    ranked = sorted(rows, key=lambda r: r["move_pct"])
    worst = ranked[0] if ranked else None
    best = ranked[-1] if ranked else None
    return {
        "key": _key, "label": label, "start": start, "end": end,
        "move_pct": move_pct, "move_value_aud": move_value_aud,
        "rows": rows, "best": best, "worst": worst,
    }


# -----------------------------------------------------------------
# Beta-weighted shock grid.
# -----------------------------------------------------------------

SHOCK_LEVELS = (-40, -20, -10, 10, 20, 40)


def portfolio_beta(weights, betas):
    """Value-weighted average beta across every holding with a
    computable beta (weight renormalized across those) - None if no
    holding has one."""
    weights = {t: w for t, w in (weights or {}).items() if w}
    num, den = 0.0, 0.0
    for t, w in weights.items():
        b = (betas or {}).get(t)
        if b is not None:
            num += b * w
            den += w
    return (num / den) if den else None


def shock_grid(beta, total_value_aud):
    """beta-weighted portfolio move (% and $) at each SHOCK_LEVELS
    index move - a plain linear beta model, not a replay; explicitly
    labelled as such by the caller."""
    if beta is None:
        return []
    out = []
    for shock_pct in SHOCK_LEVELS:
        move_pct = beta * shock_pct
        out.append({
            "shock_pct": shock_pct, "move_pct": move_pct,
            "move_value_aud": (move_pct / 100.0) * total_value_aud if total_value_aud else None,
        })
    return out


# -----------------------------------------------------------------
# Monte Carlo - 5,000 one-year paths from the historical monthly
# mean/vol/correlation matrix (numpy only).
# -----------------------------------------------------------------

def monte_carlo(weights, histories, total_value_aud, n_paths=5000, horizon_months=12, seed=None):
    """5,000 (default) simulated one-year portfolio paths, each drawn
    from a multivariate-normal model of the holdings' own historical
    monthly returns (mean vector + covariance matrix - captures the
    same volatility AND correlation each holding actually showed).
    Returns {"p5_pct","p95_pct","p5_value_aud","p95_value_aud",
    "n_holdings_used"} - None if fewer than 2 holdings have enough
    monthly history (>=12 months) to build a covariance matrix from."""
    weights = {t: w for t, w in (weights or {}).items() if w}
    monthlies = {}
    for t in weights:
        h = (histories or {}).get(t)
        m = _monthly_returns(h)
        if m is not None and len(m) >= 12:
            monthlies[t] = m
    if len(monthlies) < 1:
        return None

    df = pd.concat(monthlies, axis=1, join="inner").dropna()
    if df.empty or len(df) < 6:
        return None
    tickers = list(monthlies.keys())
    used_w_total = sum(weights[t] for t in tickers)
    if used_w_total <= 0:
        return None
    w_vec = np.array([weights[t] / used_w_total for t in tickers])

    mean_vec = df[tickers].mean().values
    cov = df[tickers].cov().values
    # A single-holding portfolio (or a degenerate all-zero-variance
    # covariance) still needs to run - np.random.multivariate_normal
    # tolerates a positive-semidefinite (not just positive-definite)
    # covariance matrix directly, so no special-casing needed here.
    rng = np.random.default_rng(seed)
    try:
        draws = rng.multivariate_normal(mean_vec, cov, size=(n_paths, horizon_months))
    except Exception:
        return None
    # draws shape: (n_paths, horizon_months, n_tickers) of simulated
    # monthly returns per holding; compound each holding's own monthly
    # draws across the horizon, then combine by weight.
    holding_growth = np.prod(1.0 + draws, axis=1)  # (n_paths, n_tickers)
    port_growth = holding_growth @ w_vec            # (n_paths,)
    port_return_pct = (port_growth - 1.0) * 100.0
    # Amendment (6 Sep) to Parts 3/13/15: the always-open Monte Carlo
    # section needs "the median tick" alongside the 5th/95th markers -
    # p50 added here (purely additive - existing p5/p95/value keys are
    # unchanged) so a result cached before this change still has
    # everything the caller needs; only the median tick is new, and the
    # renderer treats it as optional (falls back to p5/p95-only) for any
    # such pre-existing cached blob.
    p5, p50, p95 = np.percentile(port_return_pct, [5, 50, 95])
    # Owner review round fix #4 (6 Sep): the approved montecarlo_mock.html
    # shows a mini histogram of "where the 5,000 simulated years landed"
    # under the band. Purely additive, same pattern as the p50 addition
    # just above - bins the SAME port_return_pct array this function
    # already produced (no new simulation, no new randomness), so a
    # result cached before this change simply lacks "hist_counts" and
    # the renderer falls back to no histogram for that cached blob.
    _hist_counts, _hist_edges = np.histogram(port_return_pct, bins=15)
    return {
        "p5_pct": float(p5), "p95_pct": float(p95), "p50_pct": float(p50),
        "p5_value_aud": (float(p5) / 100.0) * total_value_aud if total_value_aud else None,
        "p95_value_aud": (float(p95) / 100.0) * total_value_aud if total_value_aud else None,
        "p50_value_aud": (float(p50) / 100.0) * total_value_aud if total_value_aud else None,
        "n_holdings_used": len(tickers),
        "hist_counts": [int(c) for c in _hist_counts],
        "hist_bin_edges": [float(e) for e in _hist_edges],
    }


# -----------------------------------------------------------------
# Result cache - keyed by (sorted holdings+weights) so the whole,
# fairly expensive computation above reruns only when holdings/weights
# actually change (or the cache's own TTL lapses, standing in for "or
# data refreshes" - see module docstring / the instruction's own
# wording, since this site has no explicit "nightly data refresh"
# event to hook into).
# -----------------------------------------------------------------

def cache_key(holdings):
    """Stable hash of this portfolio's (ticker, kind, shares, buy_price)
    - not just ticker+weight - so a changed share count or a newly
    added/removed holding invalidates the cache even though this
    function itself doesn't compute weights."""
    basis = sorted(
        (h.get("ticker"), h.get("kind"), round(float(h.get("shares") or 0), 6),
         round(float(h.get("buy_price") or 0), 6))
        for h in (holdings or [])
    )
    raw = json.dumps(basis, sort_keys=True).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:32]


def get_cached_result(key):
    with _conn() as conn:
        row = conn.execute(
            f"SELECT data_json, created_at FROM {_RESULT_CACHE_TABLE} WHERE cache_key = ?",
            (key,),
        ).fetchone()
    if row is None:
        return None
    created_at = datetime.fromisoformat(row[1])
    if datetime.now(timezone.utc) - created_at > timedelta(hours=RESULT_TTL_HOURS):
        return None
    try:
        return json.loads(row[0])
    except Exception:
        return None


def set_cached_result(key, data):
    with _conn() as conn:
        conn.execute(
            f"INSERT OR REPLACE INTO {_RESULT_CACHE_TABLE} (cache_key, data_json, created_at) VALUES (?, ?, ?)",
            (key, json.dumps(data), datetime.now(timezone.utc).isoformat()),
        )
