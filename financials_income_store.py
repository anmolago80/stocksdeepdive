"""
financials_income_store.py

Commit 2 of instruction_financials_income_store_and_top200_guard.md
(4 Oct 2026, Director-directed). One small JSON file per ticker on the
volume, holding the WHOLE annual income statement (every row, every
period) for a financials-mode ticker - not just the net income row,
since fcf_valuation_engine._financials_base_and_series() also feeds the
same table to _oneoff_metric_series()/_detect_distorted_years() for the
distorted-year cross-check; a store holding only net income would
silently switch that check off.

The table is always stored in the SAME currency state fundamentals_data.
get_bundle()'s own "income" table is in after its statement-to-listing-
currency conversion (see that module's own _convert_statement_currency
call site) - `currency_converted` records whether that conversion was
actually applied before this entry was written.

Three ways an entry gets filled, in cost order (see each write site's
own comment for exactly where it lives):
  1. Free - nightly_scan.analyze_ticker_lite()'s existing Step 4 one-off
     check already fetched a full bundle (and therefore its income
     table) for this exact ticker tonight; write it, no new call.
  2. Free - fundamentals_data.peek_cached_bundle(ticker) already holds a
     yfinance-sourced income table no older than tonight's own cash-flow
     statement; write it, no new call.
  3. Paid - this module's own run_nightly_prepass(), a budgeted pre-pass
     scheduler_engine._run_nightly() runs once per night (inside the
     run's own lock, before the per-universe loop) over EVERY saved
     universe's financials-mode tickers, for whichever of ways 1/2 won't
     apply tonight (a ticker not otherwise touched, e.g. a weekly-cadence
     universe not due tonight).

With this commit alone NOTHING reads this store to value anything -
see this module's own save()/get() round trip and the per-universe
"[scan] financials income: N fetched, M from store, K budget-deferred"
log line (run_nightly_prepass()'s own) for what's actually exercised.
"""

import datetime
import json
import os
import tempfile

import fundamentals_data
import scan_store
import financials_classifier

# Director's own instruction, Commit 2: "a constant,
# FINANCIALS_INCOME_FETCH_BUDGET = 150, per nightly run in total (a
# manual 'Rescan now' run gets the same budget)." Counts PAID fetches
# only (way 3 above) - ways 1/2 are free and never count against it.
FINANCIALS_INCOME_FETCH_BUDGET = 150

_STORE_DIR_NAME = "financials_income"


def _data_dir():
    return os.environ.get("RAILWAY_VOLUME_MOUNT_PATH") or os.path.dirname(__file__)


def _store_dir():
    path = os.path.join(_data_dir(), _STORE_DIR_NAME)
    try:
        os.makedirs(path, exist_ok=True)
    except OSError:
        pass
    return path


def _safe_name(ticker):
    return "".join(c if (c.isalnum() or c in "._-") else "-" for c in (ticker or "").upper())


def _path(ticker):
    return os.path.join(_store_dir(), f"{_safe_name(ticker)}.json")


def _atomic_write(path, obj):
    """Same atomic-write pattern as fundamentals_data._write_cache() -
    tmp file + os.replace() so a crash mid-write never leaves a corrupt
    entry behind. Best-effort: a write failure just means this ticker
    stays exactly as it was (missing, or the previous entry)."""
    try:
        fd, tmp_path = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".tmp_", suffix=".json")
        with os.fdopen(fd, "w") as f:
            json.dump(obj, f)
        os.replace(tmp_path, path)
    except OSError:
        pass


def save(ticker, income_df, currency=None, source="yfinance", latest_cf_period=None,
         currency_converted=True, fetched_at=None):
    """Writes (or overwrites) this ticker's entry. `income_df` is stored
    via the SAME serialisation fundamentals_data.get_bundle()'s own cache
    uses (_df_to_json/_df_from_json), per this task's own requirement -
    one shared format, not a second copy of the same logic. A fresh
    save() always clears any prior `stale` flag - this IS the refresh
    landing (see refresh_if_newer()'s own docstring)."""
    if income_df is None or getattr(income_df, "empty", True):
        return
    payload = {
        "ticker": ticker,
        "income": fundamentals_data._df_to_json(income_df),
        "currency": currency,
        "currency_converted": bool(currency_converted),
        "source": source,
        "latest_cf_period": latest_cf_period,
        "fetched_at": fetched_at or datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "stale": False,
    }
    _atomic_write(_path(ticker), payload)


def get(ticker):
    """Returns the stored entry (income_df reconstructed as a DataFrame)
    or None if there's no entry at all, or it's corrupt. Never raises."""
    path = _path(ticker)
    try:
        if not os.path.exists(path):
            return None
        with open(path) as f:
            obj = json.load(f)
        obj["income"] = fundamentals_data._df_from_json(obj.get("income"))
        return obj
    except Exception:
        return None


def mark_stale(ticker):
    """Flags an EXISTING entry stale in place, without touching its data
    - "a stale series is still used until its refresh lands" (this
    task's own rule). No-op (returns False) when there's no entry to
    flag at all; a ticker with no entry is "missing", not "stale"."""
    path = _path(ticker)
    try:
        if not os.path.exists(path):
            return False
        with open(path) as f:
            obj = json.load(f)
        if not obj.get("stale"):
            obj["stale"] = True
            _atomic_write(path, obj)
        return True
    except Exception:
        return False


def refresh_if_newer(ticker, latest_cf_period):
    """Called from the main per-ticker scan loop, which has a FRESH
    cash-flow statement in hand tonight: if this ticker's stored entry
    was fetched against an OLDER cash-flow period than the one just
    seen, flags it stale (picked up by run_nightly_prepass() as a
    priority-1 candidate on a FUTURE night - "at most one refresh
    attempt per ticker per night" holds because there is only one
    pre-pass per night and it already ran before this loop started
    tonight). A no-op when there's nothing to compare (no latest_cf_
    period given, or no existing entry, or its own latest_cf_period was
    never recorded)."""
    if not latest_cf_period:
        return
    existing = get(ticker)
    if existing is None:
        return
    stored_period = existing.get("latest_cf_period")
    if stored_period and str(latest_cf_period) > str(stored_period):
        mark_stale(ticker)


def is_stale_or_missing(ticker):
    """True when this ticker has no entry at all, or its entry is
    flagged stale - the "worth trying ways 1/2/3 for" test every write
    site (the main loop's free writes, and run_nightly_prepass()'s own
    candidate loop) checks before doing any work."""
    entry = get(ticker)
    return entry is None or bool(entry.get("stale"))


def _attempt_path(ticker):
    return os.path.join(_store_dir(), f"{_safe_name(ticker)}.deepdive_attempt.json")


def record_deep_dive_attempt(ticker, success, attempted_at=None):
    """Commit 4 of instruction_financials_income_store_and_top200_
    guard.md (4 Oct 2026, Director-directed): records a Deep Dive on-
    view fetch attempt - success OR failure - in a file SEPARATE from
    the income entry itself (a failed attempt has no income table to
    hold this timestamp in). "Deep Dive pages and /api/v1/deep-dive/
    {ticker} are hit by crawlers; without this limit every crawler
    visit to a financial with no stored table would cost a Yahoo
    call" - see deep_dive_attempted_recently()."""
    _atomic_write(_attempt_path(ticker), {
        "ticker": ticker, "success": bool(success),
        "attempted_at": attempted_at or datetime.datetime.now(datetime.timezone.utc).isoformat(),
    })


def deep_dive_attempted_recently(ticker, within_hours=24):
    """True when record_deep_dive_attempt() ran for this ticker less
    than `within_hours` ago, success or failure alike - "at most one
    [on-view] attempt per ticker per 24 hours" (this task's own rule).
    False (never blocks) on any missing/corrupt/malformed record -
    same fail-open philosophy as the rest of this module."""
    path = _attempt_path(ticker)
    try:
        if not os.path.exists(path):
            return False
        with open(path) as f:
            obj = json.load(f)
        attempted_at = obj.get("attempted_at")
        if not attempted_at:
            return False
        age = (datetime.datetime.now(datetime.timezone.utc)
               - datetime.datetime.fromisoformat(attempted_at)).total_seconds()
        return 0 <= age < within_hours * 3600
    except Exception:
        return False


def last_deep_dive_attempt(ticker):
    """Commit 5 of instruction_financials_income_store_and_top200_
    guard.md (4 Oct 2026, Director-directed): financials_dry_run.py's
    own read of record_deep_dive_attempt()'s last recorded attempt
    (success or failure) for this ticker, regardless of age - used
    only to tell a dry-run "fetch_failed" status apart from
    "awaiting_income_fetch" (no attempt recorded at all, or the only
    one recorded succeeded). Returns the stored dict ({"ticker",
    "success", "attempted_at"}) or None if there's no record. Never
    raises."""
    path = _attempt_path(ticker)
    try:
        if not os.path.exists(path):
            return None
        with open(path) as f:
            return json.load(f)
    except Exception:
        return None


def _save_direct_fetch(ticker, income_df, bundle, source):
    """Shared by run_nightly_prepass()'s way 3 and fetch_for_deep_dive():
    converts a RAW (statement-currency) direct yfinance fetch to
    listing currency using whatever fundamentals bundle already
    happens to be cached for this ticker (even a stale one - only its
    `info`'s currency fields are read, and a currency code essentially
    never changes), then saves. A ticker with no cached bundle at all
    is stored UNCONVERTED (currency_converted=False, source suffixed
    "_unconverted") - self-heals the next time this ticker is normally
    scanned (way 1, which always has a fresh `info` fetch)."""
    info = (bundle or {}).get("info") or {}
    fin_ccy = (info.get("financialCurrency") or info.get("currency") or "").upper()
    listing_ccy = (info.get("currency") or "").upper()
    if fin_ccy and listing_ccy:
        converted = (fundamentals_data._convert_statement_currency(income_df, fin_ccy, listing_ccy)
                     if fin_ccy != listing_ccy else income_df)
        save(ticker, converted, currency=listing_ccy, source=source, currency_converted=True)
    else:
        save(ticker, income_df, currency=None, source=f"{source}_unconverted",
             currency_converted=False)


def fetch_for_deep_dive(ticker):
    """Commit 4's own Deep Dive on-view fetch - ONE ticker, ONE call
    (yf.Ticker(ticker).income_stmt, never the whole get_bundle()),
    distinct from run_nightly_prepass()'s budgeted/circuit-broken batch
    fetch (way 3): a single Deep Dive view never needs that machinery,
    only record_deep_dive_attempt()'s 24h limit. Writes the entry on
    success (currency-resolved via _save_direct_fetch(), above);
    records the attempt either way. Returns the income DataFrame on
    success, None on failure - never raises."""
    import yfinance as yf

    try:
        income_df = yf.Ticker(ticker).income_stmt
    except Exception:
        income_df = None
    success = income_df is not None and not income_df.empty
    record_deep_dive_attempt(ticker, success)
    if not success:
        return None
    bundle = fundamentals_data.peek_cached_bundle(ticker)
    _save_direct_fetch(ticker, income_df, bundle, source="yfinance_deepdive")
    return income_df


# ======================================================================
# Candidate selection + the paid pre-pass (way 3).
# ======================================================================

def _candidate_rows(due_universes):
    """Every (ticker, universe, Long Score, due-tonight?, private?) row
    across EVERY saved universe (not only tonight's due ones - a weekly-
    cadence universe would otherwise take weeks to fill) whose "Moat
    Mode" is "financials" - moat_engine.compute_moat() sets that field
    from this exact same financials_classifier.is_financials() decision
    (see that module's own docstring on why the two valuation paths
    share one classifier), so this is precisely "tickers financials_
    classifier.is_financials() would say yes to today", read entirely
    from data already on disk - zero new calls."""
    due_set = set(due_universes or [])
    rows = []
    for universe in scan_store.list_saved_universes(include_private=True):
        payload = scan_store.load_scan_raw(universe, allow_private=True)
        if not payload:
            continue
        is_priv = scan_store.is_private_universe(universe)
        is_due = universe in due_set
        for row in payload.get("rows", []):
            ticker = (row.get("Ticker") or "").strip()
            if not ticker or row.get("Moat Mode") != "financials":
                continue
            rows.append({
                "ticker": ticker, "universe": universe,
                "score": row.get("Long Score") or 0,
                "is_due": is_due, "is_private": is_priv,
            })
    return rows


def _priority_group(entry, stale_tickers):
    """Director's own ordering: (1) stale series; (2) no series, PUBLIC,
    due tonight; (3) no series, other PUBLIC; (4) private. Lower number
    = higher priority."""
    if entry["ticker"] in stale_tickers:
        return 0
    if entry["is_private"]:
        return 3
    return 1 if entry["is_due"] else 2


def select_candidates(due_universes):
    """Returns (ticker_list, universes_by_ticker) - ticker_list is every
    distinct financials-mode ticker across every saved universe, ordered
    by the priority groups above and, within a group, by Long Score
    descending (Director's own "highest first" rule - this app's own
    overall ranking score; there is no separate field literally named
    "Value Score" in a saved scan row). A ticker in several universes
    (e.g. a dual-listed name, or one in both a weekly and a daily
    universe) is fetched once - the entry with the HIGHEST Long Score
    decides its priority/sort position. universes_by_ticker maps each
    ticker to the set of universes it appears in, for the per-universe
    log line in run_nightly_prepass()."""
    rows = _candidate_rows(due_universes)
    universes_by_ticker = {}
    best_by_ticker = {}
    stale_tickers = set()
    for row in rows:
        universes_by_ticker.setdefault(row["ticker"], set()).add(row["universe"])
        if row["ticker"] not in stale_tickers:
            existing_entry = get(row["ticker"])
            if existing_entry is not None and existing_entry.get("stale"):
                stale_tickers.add(row["ticker"])
        prev = best_by_ticker.get(row["ticker"])
        if prev is None or row["score"] > prev["score"]:
            best_by_ticker[row["ticker"]] = row
    ordered = sorted(
        best_by_ticker.values(),
        key=lambda e: (_priority_group(e, stale_tickers), -e["score"]),
    )
    return [e["ticker"] for e in ordered], universes_by_ticker


def run_nightly_prepass(due_universes, log=print, budget=None):
    """The paid pre-pass (way 3) - run ONCE per nightly run (a manual
    "Rescan now" gets the same budget, per this task's own rule), inside
    the caller's own job lock, BEFORE the per-universe scan loop (see
    scheduler_engine._run_nightly()'s own call site and its comment on
    why: so the SAME night's scan can already compute shadow values -
    deferred to a later commit, but this ordering is what makes that
    possible without a second pass over every universe).

    For every candidate (select_candidates(), above) already holding a
    FRESH entry: counts as "from store", no work. Otherwise tries way 2
    (fundamentals_data.peek_cached_bundle() - free) before spending any
    of `budget` on way 3 (a direct yf.Ticker(t).income_stmt fetch,
    through nightly_scan's own _yf_call_with_retry()/crumb-reset
    machinery - imported locally here, not at module level, since
    nightly_scan imports THIS module at its own top level for ways 1/2
    and a module-level import here would be circular).

    Currency: way 3's raw fetch is converted to listing currency using
    whatever fundamentals bundle already happens to be cached for this
    ticker (even a STALE one - only its `info`'s currency fields are
    used, and a currency code essentially never changes) - zero extra
    network calls. A ticker with NO cached bundle at all (never seen by
    the Compounder View) is stored UNCONVERTED, source="yfinance_direct_
    unconverted", currency_converted=False - self-heals the next time
    this ticker is normally scanned (way 1, which always has a fresh
    `info` fetch and so a known currency) on its own due night.

    Circuit breaker: stops spending the paid budget (not the free ways)
    after RATE_LIMIT_CONSECUTIVE_ABORT_THRESHOLD consecutive rate-
    limited failures - same constant and meaning as nightly_scan's own
    per-universe breaker, reused rather than re-invented.

    Logs one line per universe in `due_universes`:
    "[scan] financials income: N fetched, M from store, K budget-deferred"
    (counting a ticker once for every universe it appears in - the
    Director's own exact wording, "per universe").

    Returns {"fetched": int, "from_store": int, "budget_deferred": int,
    "breaker_tripped": bool}."""
    import yfinance as yf
    import nightly_scan

    budget = FINANCIALS_INCOME_FETCH_BUDGET if budget is None else budget
    candidates, universes_by_ticker = select_candidates(due_universes)

    outcome_by_ticker = {}
    fetched = from_store = budget_deferred = 0
    consecutive_rate_limited = 0
    breaker_tripped = False

    for ticker in candidates:
        if not is_stale_or_missing(ticker):
            outcome_by_ticker[ticker] = "from_store"
            from_store += 1
            continue

        # Way 2 (free): a cached bundle may already have a usable table.
        bundle = fundamentals_data.peek_cached_bundle(ticker)
        if bundle is not None:
            cached_income = bundle.get("income")
            cached_meta = bundle.get("meta") or {}
            if (cached_income is not None and not cached_income.empty
                    and cached_meta.get("source") == "yfinance"):
                save(ticker, cached_income, currency=(bundle.get("info") or {}).get("currency"),
                     source="yfinance_bundle_cached", currency_converted=True)
                outcome_by_ticker[ticker] = "from_store"
                from_store += 1
                continue

        if breaker_tripped or fetched >= budget:
            outcome_by_ticker[ticker] = "budget_deferred"
            budget_deferred += 1
            continue

        rate_limited_out = [False]
        income_df = nightly_scan._yf_call_with_retry(
            lambda: yf.Ticker(ticker).income_stmt, log, ticker,
            "financials income store fetch", rate_limited_out=rate_limited_out,
        )
        if income_df is None or income_df.empty:
            # Fetch failure leaves the store untouched (no entry written,
            # no existing entry touched) - counted as deferred, not fetched.
            outcome_by_ticker[ticker] = "budget_deferred"
            budget_deferred += 1
            if rate_limited_out[0]:
                consecutive_rate_limited += 1
                if consecutive_rate_limited >= nightly_scan.RATE_LIMIT_CONSECUTIVE_ABORT_THRESHOLD:
                    log("[scan] financials income: circuit breaker tripped "
                        f"({consecutive_rate_limited} consecutive rate-limited fetches) - "
                        "stopping fetches for tonight")
                    breaker_tripped = True
            else:
                consecutive_rate_limited = 0
            continue

        consecutive_rate_limited = 0
        _save_direct_fetch(ticker, income_df, bundle, source="yfinance_direct")
        outcome_by_ticker[ticker] = "fetched"
        fetched += 1

    for universe in (due_universes or []):
        tickers_here = [t for t, unis in universes_by_ticker.items() if universe in unis]
        n = sum(1 for t in tickers_here if outcome_by_ticker.get(t) == "fetched")
        m = sum(1 for t in tickers_here if outcome_by_ticker.get(t) == "from_store")
        k = sum(1 for t in tickers_here if outcome_by_ticker.get(t) == "budget_deferred")
        log(f"[scan] financials income: {n} fetched, {m} from store, {k} budget-deferred")

    return {"fetched": fetched, "from_store": from_store,
            "budget_deferred": budget_deferred, "breaker_tripped": breaker_tripped}
