"""
top100_engine.py

Top 100 tab: pure selection/scoring logic. quote_recorder.py's own
zero-Streamlit-dependency split (an "*_engine.py owns the logic, a
*_store.py owns persistence, app.py owns rendering" convention already
used across this codebase - trading_cost_engine.py/quote_snapshot_
store.py is the closest sibling) applies here too: this module never
touches Streamlit, and its only I/O is the Anthropic SDK call itself
(everything else - reading the nightly scans, reading/writing the
pool and score cache - goes through scan_store.py/top100_store.py,
passed in or read directly, matching quote_recorder.py's own precedent
of a nightly-job engine reading its own store modules directly).

THREE STAGES, each independently runnable and independently safe to
fail (a broken stage never corrupts data an earlier stage already
wrote):

  1. SELECTION (select_top100_pool) - merges every ELIGIBLE saved
     universe's scan rows (scan_store.list_saved_universes(), minus
     "imported", derived, orphaned and degraded files - see
     _eligible_scan_payloads()'s own docstring - and any currently-
     flagged/non-trading ticker), de-duplicates by ticker keeping each
     ticker's FRESHEST candidate (newest generated_at, tie-broken by
     Long Score - selection freshness fix, 30 Sep 2026, replacing the
     old "keep the MAXIMUM Long Score across every file" rule, which
     let a stale valuation win indefinitely - see POOL_MAX_FULL_SCAN_
     AGE_DAYS's own comment), recomputes its Value Score ("Long Score" -
     see snapshot_store.py's own _PUBLIC_FIELD_MAP for why that's the
     same number the rest of the site calls "Value Score") with one
     consistent formula for every ticker, keeps the top 100, and
     persists via top100_store.save_pool().

  2. SCORING (submit_nightly_batch / poll_and_ingest_batch) - a TWO-
     PHASE Batch API flow, not a single blocking call: the Batches API
     can take up to 24h to complete (most within 1h), and a nightly
     scheduler job must never block a scheduler thread anywhere near
     that long (every _run_* job in scheduler_engine.py is expected to
     return in seconds to minutes). So:
       - submit_nightly_batch(): called when there's no batch already
         in flight (top100_store.get_batch_state() is None) - selects
         up to MAX_NIGHTLY_SCORES entrants that need scoring (results-
         driven Top 100 refresh, 27 Sep 2026: a company whose most
         recently reported period has changed since its last score, a
         genuine newcomer/rubric-bump orphan, or an age-based safety
         net - see _unscored_tickers()'s own docstring for the exact
         rule), submits ONE Batches API call for all of them, and
         persists the batch id + custom_id->entrant map (each entrant's
         own score key, computed at submission time - see submit_
         nightly_batch()'s own docstring), then returns immediately. No
         scores are written yet.
       - poll_and_ingest_batch(): called first, every night, BEFORE
         submit_nightly_batch() - if a batch is in flight and its
         processing_status is "ended", parses every result, writes
         each ticker's score via top100_store.save_score(), and clears
         the batch-state row. If still processing, logs and returns
         without touching anything - tonight's run simply checks again
         tomorrow. A batch that errored/expired is logged and its
         state cleared (so a fresh batch gets submitted the next
         night) WITHOUT touching any previously cached score - "any
         failure leaves prior scores intact" is the task's own
         explicit guardrail.
     Together: night 1 polls (nothing pending) then submits; night 2
     polls (usually "ended" by now, since Batches "most complete
     within 1 hour") and ingests, then submits again if more entrants
     remain unscored.

  3. COMPOSITE (composite_score) - pure Python arithmetic over an
     already-scored ticker's ten AI dimension scores plus the pool's
     own MOS%, computed fresh every time it's read (never cached) -
     the task's own explicit instruction: "computed in Python, never
     by the model", so re-tuning WEIGHTS never needs a re-score.
"""

import json
import os
import time
from datetime import datetime, timedelta, timezone

import yfinance as yf

import fcf_valuation_engine
import financials_classifier
import market_readiness_engine
import nightly_scan
import peer_context
import ranking_engine
import resolver_engine
import scan_store
import scanner_engine
import scheduler_engine
import sector_cache_store
from share_class_engine import SHARE_CLASS_PAIRS
import top100_store

# Commit 1 (24 Sep 2026, owner-reported): first 5 errored batch results
# per ingest run get their real Anthropic error logged verbatim (type +
# message, never the request content); the rest are counted by error
# type in one summary line - see poll_and_ingest_batch()'s own comment.
_ERRORED_DETAIL_LIMIT = 5

# Commit 1 follow-up (24 Sep 2026, owner-reported): the boot diagnostic
# printed "error #1: error: " (blank) - result.result.error is an
# ErrorResponse WRAPPER whose own .type is always the literal "error"
# and which has no top-level .message at all; the real type/message
# live on its INNER .error object, which the original attribute-
# picking code never reached. _ERROR_LOG_CHAR_LIMIT bounds the full
# serialized error payload logged per result (see
# _serialize_batch_result_error() below) - generous enough to carry
# a real Anthropic error body, bounded so one pathological result
# can't flood the log.
_ERROR_LOG_CHAR_LIMIT = 500

# -----------------------------------------------------------------
# Universe selection.
# -----------------------------------------------------------------

# nightly_scan.py's own name for the TradingView-CSV import queue "virtual
# universe" - never merged into the Top 100 pool (the task's own explicit
# "imported stays excluded" instruction). Duplicated here rather than
# imported from nightly_scan.py (a heavy module with its own live-scan
# side effects at import time in some paths) - a one-line string constant
# is not worth that coupling; nightly_scan.py's own IMPORTED_UNIVERSE
# constant is the source of truth this is kept in sync with by hand.
IMPORTED_UNIVERSE = "imported"

# Pool expansion (1 Oct 2026, owner decision, Commit 5 of instruction_
# dcf_unreliable_pool_step4_nightly.md): 100 -> 200. The old cut-off sat
# near Value Score 50 and systematically excluded fairly-priced
# compounders (CPRT at 45.5 was the live case); at 200 the cut-off is
# ~40 and the Research Score, not MOS, decides where a company ranks
# within the pool. "Top 100" display text (page titles/nav/headings/
# captions/methodology, i18n.py + site_content.py) was updated to "Top
# 200" alongside this bump - no module, function, env var (TOP100_*),
# store table, key or test name was renamed.
POOL_SIZE = 200

# Top 20 Australia guaranteed-twenty (25 Sep 2026, owner-approved mock,
# "top20_australia_extended_mock.html") - the ASX EXTENSION's own target
# count. select_top100_pool() tops up rated Australians to this many
# whenever the global pool alone has fewer.
#
# Pool expansion (1 Oct 2026, owner decision): doubled 20 -> 40 alongside
# POOL_SIZE's own 100 -> 200 bump, so the ASX extension's own target stays
# proportional to the (now doubled) global pool.
TOP20_AU_TARGET = 40

# Top 100 selection freshness fix (30 Sep 2026, owner-directed). Root
# cause of the reported bug ("Top 100 still shows the old list"):
# select_top100_pool() used to keep each ticker's MAX Long Score across
# every saved universe file with no freshness check at all - a weekday-
# pinned universe (Aristocrats, S&P 600) that only gets nightly-repriced
# (never a real full rescan - reprice recomputes from the STORED
# Intrinsic Value, never a fresh DCF) kept serving a stale valuation
# indefinitely, while looking fresh because ITS OWN file's repriced_at
# kept advancing. Fixed by picking, per ticker, the candidate row with
# the NEWEST generated_at (the last full fundamentals scan) - never the
# highest Long Score alone - among ELIGIBLE candidate files only (see
# select_top100_pool()'s own docstring for exactly what "eligible"
# excludes).
#
# A row whose winning candidate is older than this many days is still
# used (there is nothing fresher to prefer), but flagged stale_
# valuation=1 so the page can disclose it rather than silently serving
# it as if it were current.
POOL_MAX_FULL_SCAN_AGE_DAYS = 10

# Tag stored on every pool row (top100_pool.pool_selection_rule) - lets
# top100_render.py's changes strip detect a selection-RULE change (as
# opposed to an ordinary night-to-night pool change) between current_
# pool() and previous_pool(), and suppress the New/Dropped/Awaiting
# diff for the one night that comparison would otherwise be artificial
# (comparing pools chosen under two different rules). Bump this string
# again if this selection rule itself changes in the future.
POOL_SELECTION_RULE = "freshest_v1"

# Top 100 Commit 3 (27 Sep 2026, owner-reported): explicit share-class
# pairs - the SAME underlying company listed under two tickers. GOOG and
# GOOGL both appeared in the 27 Sep Top 100, each fully AI-scored, because
# select_top100_pool()'s own dedupe (below) is by TICKER, and the S&P 500/
# Nasdaq 100 lists legitimately carry both classes as separate index
# members - that's correct for the Scanner/every other page, but wrong for
# a "Top 100 distinct companies" shortlist. _dedupe_share_classes() below
# collapses each pair to whichever ticker has the higher average traded
# volume BEFORE scoring, so the loser's slot goes to the next real
# candidate and the pair never both reach the AI batch.
#
# Deliberately an explicit, hand-maintained set, never inferred from
# company-name similarity (see _report_undocumented_same_name_duplicates()
# below, which only ever REPORTS a same-name pair outside this set - it
# never merges one). A ticker not listed here is never touched by the
# dedupe, however similar its company name looks to another row's.
#
# Data-correctness audit A3b (27 Sep 2026): moved to share_class_engine.py
# (imported at the top of this file) so it's the single source of truth
# for every valuation path that needs to know "is this ticker part of a
# known multi-class company" (that module's own whole_company_shares()
# reuses it too) - same name, so nothing else in this file had to change.


def _is_flagged_stale(row):
    """True if this raw scan row is the "not currently trading" ghost-
    price flag (nightly_scan.analyze_ticker_lite()'s own guard,
    "Trading Status" == "stale") - excluded from the Top 100 pool for
    the same reason every other ranking on this site already excludes
    it (Scanner/Top5/ValueMap/standout/alerts - see snapshot_store.
    flagged_stale_tickers()'s own docstring): a flagged ticker's own
    Value Score/price/MOS are exactly the numbers this guard exists to
    distrust."""
    return row.get("Trading Status") == "stale"


def _is_dcf_unreliable(row):
    """DCF-unreliable pool exclusion (1 Oct 2026, owner-directed, Part
    B finding): True when nightly_scan.py's own "DCF Unreliable" field
    (resolver_engine.dcf_looks_unreliable() - intrinsic value more than
    DCF_SANITY_MULTIPLE=3.0x the price, i.e. MOS > 66.7%) is truthy on
    this raw scan row. Excluded from the Top 100 pool for the same
    reason _is_flagged_stale() excludes a stale-price row: the Value
    Score's MOS component is clamped at 50, so a 97% MOS row collects
    the full MOS points and gets pushed into the pool purely on a model
    artefact, costing an Opus call that returns NOT RATED (8 of the 12
    names on the reported shelf were already flagged this way). Display/
    scoring-only fields (Value Score weights, MOS clamp, calculate_long_
    score(), stored scores) are untouched - this is pool ELIGIBILITY
    only, same class of rule as _is_flagged_stale()."""
    return bool(row.get("DCF Unreliable"))


def _is_fallback_financials(row):
    """Commit 4 of instruction_financials_income_store_and_top200_
    guard.md (4 Oct 2026, Director-directed), switch-gated: True when
    this row's "FCF Source" is "ocf_fallback_financials" - a
    financials-mode ticker (bank/insurer/...) whose DCF ended up on
    the OCF path anyway (no financials_income_store entry, or fewer
    than two usable positive net-income years - see fcf_valuation_
    engine.normalized_base_and_series()'s own fallback). Excluded from
    the Top 100 pool the same way a DCF-unreliable row is: operating
    cash flow for a financial includes float/deposit flows that aren't
    shareholder cash, so a fallback value is a worse base to rank/score
    from, not a genuine net-income-based valuation.

    ONLY called when financials_classifier.is_financials_store_live()
    is True (see this function's own call site) - with the switch OFF,
    "ocf_fallback_financials" is a pre-existing tag nightly_scan.py
    already produced before this instruction existed (fewer than two
    positive net-income years happens independently of the switch), so
    this exclusion must never fire then: select_top100_pool()'s output
    stays byte-identical with the switch off, fallback rows in the
    fixture or not, exactly as before this instruction."""
    return row.get("FCF Source") == "ocf_fallback_financials"


def _financials_pool_ineligible(row):
    """COMMIT B1 of instruction_top200_blank_replies_and_financials_gap.md
    (5 Oct 2026, Director-directed), switch-gated: True when this row's
    net-income path was abandoned - "FCF Source" is anything other than
    "net_income_financials" - for a FINANCIALS-MODE row. Supersedes
    _is_fallback_financials() above, which only caught the single
    source "ocf_fallback_financials" and missed "info" (the Director's
    own reported example, IVZ) and "none" (a financials-mode DCF with
    no usable base at all): the same OCF/float-pollution problem that
    motivated excluding "ocf_fallback_financials" in Commit 4 applies
    just as much to a financials-mode row that fell all the way through
    to info["freeCashflow"] or to no base at all - it is still not a
    genuine net-income-based valuation.

    "ocf_fallback_financials" is itself mode-exclusive (no standard-mode
    path ever produces it - see fcf_valuation_engine.normalized_base_
    and_series()'s own fallback), so it is excluded unconditionally,
    same as _is_fallback_financials() always did - this keeps pre-B1
    fixtures that never set "Is Financials Mode" working unchanged.
    "info" and "none" ARE shared with standard mode (an ordinary OCF-
    unavailable fallback), so for those two this only excludes the row
    when nightly_scan.py's new "Is Financials Mode" field (this
    instruction's own addition, sourced from fcf_valuation_engine.
    dcf_intrinsic_value()'s "is_financials_mode" meta) is explicitly
    True.

    ONLY called when financials_classifier.is_financials_store_live()
    is True (see this function's own call site) - with the switch OFF,
    select_top100_pool()'s output stays byte-identical, exactly as
    _is_fallback_financials() already guaranteed."""
    source = row.get("FCF Source")
    if source == "ocf_fallback_financials":
        return True
    if source in ("info", "none"):
        return bool(row.get("Is Financials Mode"))
    return False


def _dedupe_share_classes(best_by_ticker, log=print):
    """Top 100 Commit 3 (27 Sep 2026, owner-reported): collapses every
    SHARE_CLASS_PAIRS pair BOTH of whose tickers are still in
    `best_by_ticker` (mutated in place, called BEFORE pool = sorted(...)
    [:POOL_SIZE] and therefore before any scoring) down to ONE -
    whichever has the higher "avg_volume" (nightly_scan.py's own
    "Average Volume", off the already-fetched .info - see that field's
    own comment there). A None avg_volume never wins over a real
    number; if BOTH sides are unknown, falls back to the higher
    value_score (the same ordering the pool itself sorts by, so the
    tiebreak is at least consistent with how the rest of selection
    already ranks these two rows). Deleting the loser here is what
    frees its slot for the next real candidate and keeps it out of the
    AI batch entirely - nothing downstream ever sees it. Never touches
    a ticker outside SHARE_CLASS_PAIRS. Sets "also_trades_as" on the
    surviving row (the OTHER ticker it collapsed) so the page can show
    it (top100_render.py's own row header, "also trades as {ticker}") -
    absent/None on every row that isn't the winner of a real pair."""
    for pair in SHARE_CLASS_PAIRS:
        present = [t for t in pair if t in best_by_ticker]
        if len(present) < 2:
            continue

        def _volume_then_value_key(t):
            row = best_by_ticker[t]
            vol = row.get("avg_volume")
            return (vol if isinstance(vol, (int, float)) else -1, row.get("value_score") or 0)

        present.sort(key=_volume_then_value_key, reverse=True)
        winner, *losers = present
        for loser in losers:
            log(f"[top100] share-class dedupe: kept {winner} (avg vol "
                f"{best_by_ticker[winner].get('avg_volume')!r}) over {loser} (avg vol "
                f"{best_by_ticker[loser].get('avg_volume')!r}) - same company, per "
                f"SHARE_CLASS_PAIRS")
            del best_by_ticker[loser]
        best_by_ticker[winner]["also_trades_as"] = losers[0]


def _report_undocumented_same_name_duplicates(best_by_ticker, log=print):
    """Top 100 Commit 3 (27 Sep 2026, owner-reported): after
    _dedupe_share_classes() above has already collapsed every
    DOCUMENTED pair down to one ticker, groups whatever tickers remain
    by normalized company name - any group still holding more than one
    ticker is a same-name duplicate SHARE_CLASS_PAIRS doesn't cover.
    Report-only, exact-name match (deliberately never fuzzy - "never
    merge on company-name similarity alone" applies doubly to even
    DETECTING a merge candidate this way): logs once per group, never
    removes or merges anything itself. Reviewed by hand; a genuine pair
    gets added to SHARE_CLASS_PAIRS explicitly, never auto-added here."""
    by_name = {}
    for ticker, row in best_by_ticker.items():
        name = (row.get("company_name") or "").strip().lower()
        if not name:
            continue
        by_name.setdefault(name, []).append(ticker)
    for name, tickers in by_name.items():
        if len(tickers) > 1:
            log(f"[top100] same company name, NOT in SHARE_CLASS_PAIRS - review and add "
                f"by hand if genuine: {sorted(tickers)} all show company_name={name!r}")


def is_top200_excluded(universe):
    """Director's next round, item 3 (8 Oct 2026, instruction_health_
    fixes_chart_and_new_markets.md): "a Top 200 exclusion separate
    from privacy: a list of universes that may be public on the
    Scanner but are kept out of the Top 200." A universe's PRIVACY
    (scan_store.is_private_universe() - whether its rows are visible
    to anyone but the owner at all) and its TOP 200 ELIGIBILITY
    (this function) are two independent levers: making TSX 60 public
    on the Scanner must never, by itself, let it start winning Top
    200 slots - that needs its OWN, separate go-ahead.

    TOP200_EXCLUDED_UNIVERSES env var (comma-separated), re-read on
    every call (same convention as every other switch in this
    module), overrides the code default entirely when SET (even to
    an empty string - "nothing is excluded" is a valid explicit
    choice). Unset: the code default is DERIVED, not a hand-
    maintained name list - "every non-US, non-Australian universe"
    (the Director's own words), i.e. not in scanner_engine.
    AUSTRALIA_UNIVERSES or scanner_engine.USA_UNIVERSES. Deriving it
    this way (rather than hand-listing FTSE 100/TSX 60/.../DAX/...)
    means a BRAND NEW non-US/non-AU universe a future part adds is
    automatically Top-200-excluded from day one, with no name to
    remember to add anywhere."""
    raw = os.environ.get("TOP200_EXCLUDED_UNIVERSES")
    if raw is not None:
        excluded = {u.strip() for u in raw.split(",") if u.strip()}
        return universe in excluded
    return universe not in scanner_engine.AUSTRALIA_UNIVERSES and universe not in scanner_engine.USA_UNIVERSES


def _eligible_scan_payloads(log=print):
    """Top 100 selection freshness fix (30 Sep 2026, owner-directed):
    {universe: payload} for every saved universe file that's actually
    ELIGIBLE to win a ticker's pool slot - loaded via scan_store.
    load_scan_raw() ONCE per file (never re-opened), so the caller can
    read generated_at/repriced_at/attention_lite/degraded straight off
    the same payload dict it already has, with no second read.

    Excluded, each logged once with a reason:
      - "imported" (unchanged - never part of Top 100 at all).
      - DERIVED universes (scheduler_engine._DERIVED_UNIVERSE_PARENTS) -
        a derived file (e.g. "ASX 100") is itself just a filtered VIEW
        of its parent(s)' own rows (e.g. "ASX 200"/"ASX 300"), rebuilt
        from whatever the parents' OWN generated_at already is - letting
        it win a ticker slot in its own right would only ever duplicate
        or shadow the parent's own freshness, never add real information.
      - PRIVATE universes (scan_store.is_private_universe()).
      - TOP-200-EXCLUDED universes (is_top200_excluded(), above) - a
        SEPARATE lever from privacy, next round item 3: a universe can
        be public on the Scanner and still never compete for a Top 200
        slot.
      - ORPHANED universes - a saved file whose universe name isn't in
        today's live NIGHTLY_UNIVERSES cadence map at all (renamed or
        retired since that file was written) is no longer maintained by
        anything; its rows can silently age forever with nothing to ever
        refresh them, so they're excluded from winning outright rather
        than competing on a frozen "freshness" that will never improve.
      - `degraded` files (scan_store.save_scan()'s own flag - completed
        for fewer tickers than its own completeness threshold, kept only
        because no better prior scan existed) - a partial scan is exactly
        the kind of lower-confidence data this fix exists to stop from
        silently winning a slot over a complete, if slightly older, one.
    """
    cadence_map = scheduler_engine.nightly_universe_cadence()
    out = {}
    # Stage 1b (3 Oct 2026, Director-directed): list_saved_universes()'s
    # own default (include_private=False) already excludes a private
    # universe silently - include_private=True here instead, so this
    # function can log the skip explicitly (same visibility as the
    # "orphaned" skip just below) rather than the candidate simply never
    # appearing with no trace of why.
    for universe in scan_store.list_saved_universes(include_private=True):
        if universe == IMPORTED_UNIVERSE:
            continue
        if universe in scheduler_engine._DERIVED_UNIVERSE_PARENTS:
            continue
        if scan_store.is_private_universe(universe):
            log(f"[top100] skipped private universe {universe!r}")
            continue
        if is_top200_excluded(universe):
            log(f"[top100] skipped universe {universe!r} - excluded from the Top "
                f"200 (TOP200_EXCLUDED_UNIVERSES; separate from its privacy status)")
            continue
        if universe not in cadence_map:
            log(f"[top100] selection: skipping {universe!r} - orphaned "
                f"(not in the current NIGHTLY_UNIVERSES cadence map)")
            continue
        payload = scan_store.load_scan_raw(universe)
        if not payload:
            continue
        if payload.get("degraded"):
            log(f"[top100] selection: skipping {universe!r} - degraded scan")
            continue
        out[universe] = payload
    return out


def _recompute_value_score(row):
    """Top 100 selection freshness fix, 2.2 (30 Sep 2026, owner-
    directed): "one ranking rule" - Long Score recomputed from the
    chosen row's own stored components with discovery_measured=False
    for EVERY ticker, the only rule available for every universe (a
    lite-scanned universe's own stored Long Score never had a real
    Discovery signal to measure in the first place - see ranking_
    engine.calculate_long_score()'s own discovery_measured docstring -
    while a full-attention universe's stored Long Score DID use a real
    one; blending the two AS STORED would rank a lite row and a full
    row for the same company under two different formulas). Never
    raises - a row missing Quality/MOS entirely (shouldn't happen for
    anything that reached this far) resolves via calculate_long_score's
    own None-tolerant clamping, same as every other caller."""
    return ranking_engine.calculate_long_score(
        row.get("Quality"), row.get("MOS %") or 0.0, row.get("Psychology"),
        row.get("Discovery (lite)") or 0, discovery_measured=False,
    )


def _build_best_by_ticker(log=print):
    """The deduped, eligibility-filtered candidate pool select_top100_
    pool() builds every call - extracted into its own function (COMMIT
    3 of instruction_top200_unrated_and_blank_replies_combined.md, 5
    Oct 2026) purely so backfill_preview() can read the SAME merit-
    ordered candidate universe select_top100_pool() itself would use
    tonight, without duplicating a single line of this logic. No
    behaviour change versus the inline version this replaced - same
    eligible-universe read, same per-row DCF-unreliable/fallback-
    financials exclusion, same freshest-row-wins/Long-Score-tiebreak
    best_candidate selection, same recomputed Value Score, same share-
    class dedupe and same-name-duplicate report. Read-only: reads
    already-saved scan files (scan_store, via _eligible_scan_payloads())
    and the financials-mode switch, writes nothing, calls no network -
    identical safety profile to select_top100_pool() itself before its
    own first write."""
    eligible = _eligible_scan_payloads(log=log)
    now = datetime.now(timezone.utc)

    # ticker -> the winning candidate's (generated_at datetime, row's own
    # stored Long Score, row dict, universe name, generated_at raw string)
    # - kept as a single running "best so far" per ticker (freshest first,
    # Long Score tie-break) rather than a list of every candidate, since
    # nothing downstream needs the losing candidates.
    best_candidate = {}
    # DCF-unreliable pool exclusion (1 Oct 2026, owner-directed; extended
    # 2 Oct 2026 after the 23:00 UTC crash - see the now-removed assert
    # near the end of this function for what broke). A row is excluded
    # here if EITHER nightly_scan.py's stored "DCF Unreliable" flag is
    # truthy (scan-time price) OR resolver_engine.dcf_looks_unreliable()
    # is true against the row's CURRENT raw Intrinsic Value/Price (which
    # can differ from scan time - nightly repricing runs between scan-
    # save and this selection). Checking both here, before best_candidate
    # is ever built, means a row can never reach the pool on a stale
    # "looked fine at scan time" flag - closing the staleness race that
    # let 'CARG' through the old per-row skip and into the (now deleted)
    # end-of-function assert, crashing the whole job with no pool saved.
    # Counts both reasons separately for the summary log line below; the
    # distinct excluded tickers (insertion order, capped at 20 for the
    # log line) are shared across both reasons, same cap the old single-
    # reason line already used. A ticker excluded here from ONE
    # universe's row can still enter the pool via a different universe's
    # clean row for the same ticker, same as any other per-row skip.
    dcf_unreliable_excluded_count = 0
    dcf_unreliable_excluded_tickers = []
    dcf_unreliable_by_stored_flag = 0
    dcf_unreliable_by_current_price = 0
    # Commit 4 of instruction_financials_income_store_and_top200_
    # guard.md: read ONCE per call, not per row - is_financials_store_
    # live() is a cheap env lookup but there's no reason to repeat it
    # for every row in every universe.
    _financials_store_live = financials_classifier.is_financials_store_live()
    fallback_financials_excluded_count = 0
    fallback_financials_excluded_tickers = []
    for universe, payload in eligible.items():
        gen_raw = payload.get("generated_at")
        try:
            gen_dt = datetime.fromisoformat(gen_raw) if gen_raw else None
        except ValueError:
            gen_dt = None
        if gen_dt is None:
            # No usable timestamp at all (shouldn't happen for a file
            # save_scan() itself wrote) - treat as "as old as possible"
            # so a ticker with any other timestamped candidate always
            # prefers it, never this one.
            gen_dt = datetime.min.replace(tzinfo=timezone.utc)
        for row in payload.get("rows") or []:
            # Defensive per-row guard (2 Oct 2026, owner-directed): the
            # Top 100 job must never crash on one bad row's data - any
            # exception evaluating a single row's eligibility is logged
            # and that row is skipped, not raised, so one malformed
            # candidate can never take down the entire nightly selection
            # the way the old end-of-function assert did.
            try:
                ticker = (row.get("Ticker") or "").strip().upper()
                row_long_score = row.get("Long Score")
                if not ticker or row_long_score is None:
                    continue
                if _is_flagged_stale(row):
                    continue
                stored_unreliable = _is_dcf_unreliable(row)
                current_unreliable = resolver_engine.dcf_looks_unreliable(
                    row.get("Intrinsic Value"), row.get("Price"))
                if stored_unreliable or current_unreliable:
                    dcf_unreliable_excluded_count += 1
                    if stored_unreliable:
                        dcf_unreliable_by_stored_flag += 1
                    else:
                        dcf_unreliable_by_current_price += 1
                    if ticker not in dcf_unreliable_excluded_tickers:
                        dcf_unreliable_excluded_tickers.append(ticker)
                    continue
                if _financials_store_live and _financials_pool_ineligible(row):
                    fallback_financials_excluded_count += 1
                    if ticker not in fallback_financials_excluded_tickers:
                        fallback_financials_excluded_tickers.append(ticker)
                    continue
                prev = best_candidate.get(ticker)
                key = (gen_dt, row_long_score)
                if prev is None or key > (prev[0], prev[1]):
                    best_candidate[ticker] = (gen_dt, row_long_score, row, universe, gen_raw)
            except Exception as e:
                log(f"[top100] selection: skipping row with eligibility-check "
                    f"error ({row.get('Ticker', '?')!r} in {universe}): {e}")
                continue

    if dcf_unreliable_excluded_count:
        _shown = dcf_unreliable_excluded_tickers[:20]
        _more = f" (+{len(dcf_unreliable_excluded_tickers) - 20} more)" if len(dcf_unreliable_excluded_tickers) > 20 else ""
        log(f"[top100] selection: excluded {dcf_unreliable_excluded_count} "
            f"DCF-unreliable row(s) ({dcf_unreliable_by_stored_flag} by stored flag, "
            f"{dcf_unreliable_by_current_price} by current price): {', '.join(_shown)}{_more}")

    if fallback_financials_excluded_count:
        _fshown = fallback_financials_excluded_tickers[:20]
        _fmore = (f" (+{len(fallback_financials_excluded_tickers) - 20} more)"
                  if len(fallback_financials_excluded_tickers) > 20 else "")
        log(f"[top100] selection: excluded {fallback_financials_excluded_count} "
            f"fallback-financials row(s): {', '.join(_fshown)}{_fmore}")

    best_by_ticker = {}
    for ticker, (gen_dt, row_long_score, row, universe, gen_raw) in best_candidate.items():
        age_days = (now - gen_dt).total_seconds() / 86400.0
        stale_valuation = age_days > POOL_MAX_FULL_SCAN_AGE_DAYS
        recomputed_value_score = _recompute_value_score(row)
        best_by_ticker[ticker] = {
                    "ticker": ticker,
                    "company_name": row.get("Company Name"),
                    "universe": universe,
                    "value_score": recomputed_value_score,
                    # Audit trail (2.2): the winning row's own stored
                    # Long Score, BEFORE the discovery_measured=False
                    # recompute above - never read by any sort/
                    # selection/composite_score(), display/debugging
                    # only.
                    "value_score_source_row": row_long_score,
                    # Freshness fields (this fix's own load-bearing
                    # addition) - generated_at is the winning candidate's
                    # own file timestamp; source_universe_generated_at is
                    # kept identical to it today (there is only ever one
                    # source per selection), added as its own column so
                    # a future selection rule that draws a row's display
                    # fields and its generated_at from two DIFFERENT
                    # places has somewhere to record that divergence
                    # without another schema change.
                    "generated_at": gen_raw,
                    "source_universe_generated_at": gen_raw,
                    "stale_valuation": stale_valuation,
                    "pool_selection_rule": POOL_SELECTION_RULE,
                    "mos_pct": row.get("MOS %"),
                    "price": row.get("Price"),
                    "intrinsic_value": row.get("Intrinsic Value"),
                    "currency": "AUD" if ticker.endswith(".AX") else "USD",
                    # v2 amendment: the raw scan row's own "Psychology"
                    # number (nightly_scan.py's fear-greed-fomo score,
                    # same field snapshot_store._PUBLIC_FIELD_MAP already
                    # exposes publicly) - carried through so the page can
                    # show a free, NEVER-SCORED sentiment chip (fearful/
                    # neutral/greedy) without re-deriving or re-fetching
                    # anything. Never fed into composite_score() - purely
                    # a display read, same "display flag only" status as
                    # the tradability chip's own spread%.
                    "psychology": row.get("Psychology"),
                    # Top 100 Commit 5 (25 Sep 2026, owner-reported,
                    # industry mock): the sector tag, same "carried
                    # through for a display-only tag, never fed into
                    # composite_score()" status as psychology just above
                    # - the page refines it with the Deep Dive's own
                    # finer industry string where cached (top100_render.
                    # _finer_industry_by_ticker()).
                    #
                    # Sector fallback (27 Sep 2026, owner-reported):
                    # scanner_engine.sector_for_ticker(ticker, scan_row=
                    # row) instead of the bare row.get("Sector") this
                    # used to be - identical result for every ticker
                    # whose WINNING row already carries a "Sector" (the
                    # common case, unchanged), but for a ticker whose
                    # winning row came from a sector-less universe (e.g.
                    # Nasdaq 100 via the Invesco CSV/static fallback,
                    # Russell lists - see nightly_scan.run_universe_
                    # scan()'s own _sector_by_ticker comment) this falls
                    # through to the site's own persistent sector_cache_
                    # store (written through by every OTHER universe's
                    # scan that DOES carry a sector for the same ticker),
                    # then the ASX static map - so an S&P 500 member that
                    # also happens to be a Nasdaq 100 constituent no
                    # longer loses its sector just because the Nasdaq 100
                    # row happened to win on Value Score. company_info is
                    # deliberately omitted here (no per-ticker network
                    # call at THIS point in selection) - a ticker still
                    # blank after this falls to the bounded one-shot
                    # yfinance fill below (_fill_missing_sectors()).
                    "sector": scanner_engine.sector_for_ticker(ticker, scan_row=row),
                    # Dividend yield display (26 Sep 2026, owner-approved
                    # mock): the raw scan row's own "Dividend Yield %"
                    # (nightly_scan.py's Dividend TTM / Price), same
                    # "carried through for display only, never fed into
                    # composite_score() or any sort/selection" status as
                    # psychology/sector above.
                    "dividend_yield_pct": row.get("Dividend Yield %"),
                    # Results-driven Top 100 refresh (27 Sep 2026,
                    # owner-directed): the raw scan row's own "Most
                    # Recent Quarter" (nightly_scan.py's ISO-date read of
                    # yfinance's mostRecentQuarter), same "carried
                    # through" status as dividend_yield_pct above - but
                    # unlike every prior carried-through field, this one
                    # is NOT display-only: top100_engine._unscored_
                    # tickers() reads it to decide whether a pooled
                    # company needs a fresh AI score. Still never fed
                    # into composite_score()/any sort/selection here.
                    "most_recent_quarter": row.get("Most Recent Quarter"),
                    # Top 100 Commit 3 (27 Sep 2026, owner-reported): the
                    # raw scan row's own "Average Volume" - dedupe input
                    # only (_dedupe_share_classes() below), never fed
                    # into composite_score()/any sort/selection here.
                    "avg_volume": row.get("Average Volume"),
                }

    _dedupe_share_classes(best_by_ticker, log=log)
    _report_undocumented_same_name_duplicates(best_by_ticker, log=log)
    return best_by_ticker, len(eligible)


def select_top100_pool(log=print):
    """Merges every ELIGIBLE saved universe's scan rows (see
    _eligible_scan_payloads() above for exactly what's excluded and
    why), de-duplicates by ticker, excludes any currently-flagged/non-
    trading row, keeps the top POOL_SIZE by (recomputed) Value Score,
    and persists the result via top100_store.save_pool(as_of=today's
    UTC date). Returns the saved GLOBAL pool only (unchanged contract -
    the ASX extension below is never part of this return value) -
    [{"ticker","company_name","universe","value_score","mos_pct",
    "price","intrinsic_value","currency","psychology","sector",
    "dividend_yield_pct","most_recent_quarter","generated_at",
    "stale_valuation"}, ...], Value Score descending. Never raises -
    a single bad universe file is skipped (scan_store.load_scan_raw()
    itself already returns None on any read error), and an empty
    result (no saved scans yet) simply persists/returns an empty pool
    rather than crashing.

    Top 100 selection freshness fix (30 Sep 2026, owner-directed):
    per ticker, picks the candidate row with the NEWEST generated_at
    (the last full fundamentals scan) among every eligible file that
    carries this ticker - NEVER the highest Long Score alone, which is
    what let a stale valuation win indefinitely (see this module's own
    POOL_MAX_FULL_SCAN_AGE_DAYS comment for the full root cause). Ties
    on generated_at (same file, or two files scanned in the same
    second) break on the row's own stored Long Score. A ticker whose
    every candidate is older than POOL_MAX_FULL_SCAN_AGE_DAYS still
    gets its freshest available candidate (there is nothing better to
    prefer) - flagged stale_valuation=1 so the page can disclose it,
    the row is never dropped or hidden for this alone. Value Score
    itself is then RECOMPUTED (see _recompute_value_score() above) -
    the winning row's own original stored Long Score is kept alongside
    as value_score_source_row, for audit.

    ASX EXTENSION (Top 20 Australia guaranteed-twenty, 25 Sep 2026,
    owner-approved mock): the global pool/selection above is otherwise
    completely untouched - pure global merit, exactly as before. After
    it's computed, if the pool's own .AX members fall short of
    TOP20_AU_TARGET, the next-best ASX companies by Value Score (same
    dedupe/exclusion rules, drawn from the same `best_by_ticker`
    candidate set, excluding anything already in the pool) are
    persisted ALONGSIDE it with asx_extension=True - a purely additive
    top100_pool column (top100_store's own guarded-ALTER-TABLE
    convention, same as psychology/sector). top100_store.current_pool()/
    previous_pool() both filter asx_extension out unconditionally, so
    every existing reader (Full 100, Mixed, USA, the homepage teaser,
    the changes strip, any exactly-100 assertion) is unaffected by its
    existence - only top100_store.current_asx_extension() and
    top100_render.py's own Australia-tab code ever read it."""
    # Pool expansion one-off persistence seed (1 Oct 2026, owner decision,
    # Commit 5): captured BEFORE anything below mutates top100_pool - at
    # this point top100_store.current_pool()/current_asx_extension() are
    # still LAST NIGHT's saved selection (today's save_pool() call, later
    # in this function, hasn't run yet) - see _seed_pool_expansion_once()
    # below for what this baseline is used for and why it's marker-
    # guarded to fire exactly once (the first run under the 100->200
    # bump), not every night.
    _pool_expansion_pending = not os.path.exists(_pool_expansion_v200_marker_path())
    _pool_expansion_baseline = (
        {r["ticker"] for r in top100_store.current_pool()} |
        {r["ticker"] for r in top100_store.current_asx_extension()}
        if _pool_expansion_pending else set()
    )

    best_by_ticker, eligible_count = _build_best_by_ticker(log=log)

    # COMMIT 3 of instruction_top200_unrated_and_blank_replies_combined.md
    # (5 Oct 2026, Director-directed, owner-approved for build+push-on-
    # go, switch OFF by default): `_merit_ordered` is EXACTLY today's
    # pre-slice list - with the switch OFF, `pool` below is sliced from
    # it identically to before this commit (byte-identical to 007d669).
    # With the switch ON, _apply_backfill() walks the SAME list and
    # swaps in the next-in-line candidate for any UNRATED_MODEL/
    # UNRATED_FAILED one, within TOP200_BACKFILL_MAX positions beyond
    # POOL_SIZE. Either way the dry-run computation below (switch OFF)
    # uses the identical _apply_backfill()/_backfill_summary() pair the
    # live switch-ON path would use, so the owner preview and the real
    # thing can never disagree.
    _merit_ordered = sorted(best_by_ticker.values(), key=lambda r: r["value_score"], reverse=True)
    _set_aside_rows = []
    if is_backfill_live():
        _backfill = _apply_backfill(_merit_ordered, model=MODEL_TOP100)
        # A3 amendment of instruction_top200_amendments_and_currency_
        # view.md (5 Oct 2026, Director-directed): standins are merged
        # straight into `pool` - ordinary pool members from here on
        # (no new column, no special flag), so they're saved via the
        # same save_pool() call below, picked up by update_pool_
        # presence()'s own re-score-trigger bookkeeping automatically,
        # and rendered by the UNCHANGED public page exactly like any
        # other RATED row: current_pool() includes them, _sort_and_
        # gate_rated() sorts every RATED row (standin or not) by
        # composite together, and a WAITING row's own composite is
        # already None so it's already excluded from that same sort -
        # no new rendering code needed for the ranked list itself.
        pool = _backfill["counted"] + _backfill["standins"]
        for _row, _status in _backfill["set_aside"]:
            _row["backfill_set_aside"] = True
            _row["backfill_set_aside_status"] = _status
            _set_aside_rows.append(_row)
        _summary = _backfill_summary(_merit_ordered, _backfill, model=MODEL_TOP100)
        if _backfill["short"]:
            log(f"[top100] backfill WARNING: only {len(_backfill['counted'])} of {POOL_SIZE} "
                f"places filled within {POOL_SIZE + TOP200_BACKFILL_MAX} candidates")
        log(f"[top100] backfill: set aside {len(_summary['set_aside_model']) + len(_summary['set_aside_failed'])} "
            f"(model {len(_summary['set_aside_model'])}, failed {len(_summary['set_aside_failed'])}): "
            f"{', '.join(sorted(_summary['set_aside_model'] + _summary['set_aside_failed']))}; "
            f"pulled in {len(_summary['pulled_in'])}: {', '.join(_summary['pulled_in'])}; "
            f"new to score {_summary['new_to_score']}; "
            f"public list {_summary['public_list_count']} of {POOL_SIZE} "
            f"(scored-set rated {_summary['scored_set_rated']} + stand-ins {len(_summary['standin_tickers'])}); "
            f"waiting {_summary['scored_set_waiting']}; "
            f"set aside {len(_summary['set_aside_model']) + len(_summary['set_aside_failed'])}")
    else:
        pool = _merit_ordered[:POOL_SIZE]
        try:
            _dry_backfill = _apply_backfill(_merit_ordered, model=MODEL_TOP100)
            _dry_summary = _backfill_summary(_merit_ordered, _dry_backfill, model=MODEL_TOP100)
            log(f"[top100] backfill dry run: would set aside "
                f"{len(_dry_summary['set_aside_model']) + len(_dry_summary['set_aside_failed'])} "
                f"(model {len(_dry_summary['set_aside_model'])}, failed {len(_dry_summary['set_aside_failed'])}): "
                f"{', '.join(sorted(_dry_summary['set_aside_model'] + _dry_summary['set_aside_failed']))}; "
                f"would pull in {len(_dry_summary['pulled_in'])}: {', '.join(_dry_summary['pulled_in'])}; "
                f"new to score {_dry_summary['new_to_score']}, est. cost ${_dry_summary['est_cost_usd']:.4f}; "
                f"public list would show {_dry_summary['public_list_count']} of {POOL_SIZE} "
                f"(scored-set rated {_dry_summary['scored_set_rated']} + "
                f"stand-ins {len(_dry_summary['standin_tickers'])}); "
                f"waiting {_dry_summary['scored_set_waiting']}; "
                f"set aside {len(_dry_summary['set_aside_model']) + len(_dry_summary['set_aside_failed'])}")
        except Exception as e:
            log(f"[top100] backfill dry run failed (non-fatal): {e}")
    as_of = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # COMMIT 3: backfill_set_aside is deliberately NEVER set explicitly
    # on `pool`/`extension` rows here (save_pool() already defaults it
    # to False via r.get("backfill_set_aside") when the key is absent)
    # - adding the key to these dicts would change select_top100_
    # pool()'s own JSON return value even with the switch OFF, breaking
    # the task's own "byte-identical to 007d669" requirement for a key
    # that every caller already treats as falsy by its absence.
    pool_tickers = {r["ticker"] for r in pool}
    for row in pool:
        row["asx_extension"] = False

    # COMMIT 3: a set-aside (UNRATED_MODEL/UNRATED_FAILED) ticker is
    # excluded from ASX-extension eligibility too ("same principle: an
    # unrated company never takes one of the 20 places" - the task's
    # own wording for the Top 20 tabs) - this is the one unambiguous
    # part of that principle to apply here; walking further down the
    # merit order to BACKFILL a replacement for the extension's own 40
    # target is left unchanged (ambiguous - see the report's own Top 20
    # tabs section for the two options). Without this exclusion the
    # same row object could end up claimed by both `extension` and
    # `_set_aside_rows`, producing two conflicting save_pool() upserts
    # for the same (as_of, ticker).
    _set_aside_tickers = {r["ticker"] for r in _set_aside_rows}
    au_in_pool = sum(1 for t in pool_tickers if t.endswith(".AX"))
    extension = []
    if au_in_pool < TOP20_AU_TARGET:
        needed = TOP20_AU_TARGET - au_in_pool
        _au_sorted_candidates = sorted(
            (r for t, r in best_by_ticker.items()
             if t.endswith(".AX") and t not in pool_tickers and t not in _set_aside_tickers),
            key=lambda r: r["value_score"], reverse=True,
        )
        au_candidates = _au_sorted_candidates[:needed]
        # A3 amendment of instruction_top200_amendments_and_currency_
        # view.md (5 Oct 2026, Director-directed, "the same rule
        # applies to the Top 20 tabs and the ASX extension") - the
        # SAME stand-in rule as the main 200: if the extension's own
        # `needed` slots contain WAITING members, walk further down
        # the SAME AU-only merit order for up to that many further
        # RATED-only AU standins, so the Top 20 Australia tab can
        # still show its own guaranteed 40 wherever enough RATED AU
        # candidates exist. Only when the switch is actually live -
        # with it OFF there are no set-aside tickers to exclude in the
        # first place, and this must never fire for the switch-OFF
        # byte-identical-to-007d669 guarantee.
        if is_backfill_live():
            _au_latest = top100_store.latest_scores_for_model(MODEL_TOP100, RUBRIC_VERSION)
            _au_failures = top100_store.score_failures_for_model(MODEL_TOP100, RUBRIC_VERSION)
            _au_waiting_count = sum(
                1 for row in au_candidates
                if company_status(_au_latest.get(row["ticker"]), _au_failures.get(row["ticker"])) == "waiting"
            )
            _au_already = {r["ticker"] for r in au_candidates}
            _au_standins = _standins_from_remaining(
                _au_sorted_candidates[needed:], _au_already, _set_aside_tickers,
                _au_waiting_count, model=MODEL_TOP100)
            au_candidates = au_candidates + _au_standins
        for row in au_candidates:
            row["asx_extension"] = True
        extension = au_candidates

    for _row in _set_aside_rows:
        _row["asx_extension"] = False

    # (1 Oct 2026 owner-directed defensive assert removed 2 Oct 2026: it
    # re-derived this same dcf_looks_unreliable() check from the curated
    # intrinsic_value/price pair and raised if it ever fired, on the
    # premise that the per-row skip above made it unreachable. It
    # wasn't - the per-row skip only checked the scan-time "DCF
    # Unreliable" flag, and a row's price can be repriced between scan-
    # save and this function running, so a row could legitimately
    # become unreliable-by-current-price after the old skip ran but
    # before this assert, crashing the whole nightly job (no pool
    # saved) on 'CARG' at 23:00 UTC on 1 Oct. The fix is upstream, not
    # here: the per-row loop above now also checks current price, so a
    # row like that is excluded before best_candidate is ever built and
    # never reaches this point - no assert needed, and a hard crash is
    # never an acceptable outcome for a data-staleness condition.)

    _fill_missing_sectors(pool + extension + _set_aside_rows, log=log)
    top100_store.save_pool(pool + extension + _set_aside_rows, as_of)

    if _pool_expansion_pending:
        # Pool expansion one-off persistence seed (1 Oct 2026, owner
        # decision, Commit 5): every ticker in TONIGHT's selection that
        # wasn't in the baseline captured above (last night's saved 100/
        # ASX-20 pool) is a brand-new entrant purely because the pool
        # grew 100->200 - seeded straight to consecutive_nights=3 so the
        # newcomer-persistence-filter gate (NEWCOMER_PERSISTENCE_NIGHTS,
        # three nights) doesn't defer scoring the whole expansion by
        # three nights, same marker-guarded one-off pattern as seed_pool_
        # presence_for_v6_once() above. Called BEFORE update_pool_
        # presence() below (not after): top100_store.seed_pool_presence()
        # is an INSERT OR IGNORE, so once it writes last_seen_utc_date=
        # as_of/consecutive_nights=3 for an expansion ticker, update_pool_
        # presence()'s own "last_seen_utc_date IS utc_date already -> left
        # unchanged" rule leaves that 3 untouched a moment later - doing
        # it the other way around would let update_pool_presence() write
        # consecutive_nights=1 FIRST (no prior row), and then this seed's
        # own INSERT OR IGNORE would no-op against that existing row,
        # leaving the expansion gated exactly as this seed exists to
        # prevent. Genuine churn from tomorrow night onward is gated as
        # normal - this block never fires again once its marker exists.
        try:
            tonight_tickers = {r["ticker"] for r in pool} | {r["ticker"] for r in extension}
            expansion_tickers = sorted(tonight_tickers - _pool_expansion_baseline)
            if expansion_tickers:
                top100_store.seed_pool_presence(
                    {t: NEWCOMER_PERSISTENCE_NIGHTS for t in expansion_tickers}, as_of,
                )
            log(f"[top100] pool expansion: seeded {len(expansion_tickers)} "
                f"expansion tickers past the persistence gate")
        except Exception as e:
            log(f"[top100] pool expansion seeding failed: {e}")
        finally:
            try:
                with open(_pool_expansion_v200_marker_path(), "w") as f:
                    f.write(datetime.now(timezone.utc).isoformat())
            except OSError as e:
                log(f"[top100] pool expansion seeding: could not write marker file: {e}")

    try:
        # Newcomer persistence filter (1 Oct 2026, owner decision): every
        # ticker in TONIGHT's selection (pool + extension) gets its
        # pool_presence streak updated here, right after the selection
        # itself is persisted - see top100_store.update_pool_presence()'s
        # own docstring for the increment/reset rule. Wrapped in its own
        # try/except: a failure here must never fail pool selection
        # itself over a display/gating-only side table.
        top100_store.update_pool_presence([r["ticker"] for r in pool + extension], as_of)
    except Exception as e:
        log(f"[top100] could not update pool presence: {e}")
    stale_count = sum(1 for r in pool if r.get("stale_valuation"))
    log(f"[top100] selected {len(pool)} companies for {as_of} "
        f"(from {len(best_by_ticker)} deduped candidates across "
        f"{eligible_count} eligible universes, freshest-row-wins); "
        f"stale valuations (>{POOL_MAX_FULL_SCAN_AGE_DAYS}d, no fresher candidate): {stale_count}; "
        f"ASX extension: {len(extension)} added ({au_in_pool} pool Australians -> "
        f"{au_in_pool + len(extension)} total for Top 20 Australia)")
    # COMMIT 1 of instruction_top200_unrated_and_blank_replies_combined.md
    # (5 Oct 2026, Director-directed): "after every selection" half of
    # the task's own coverage log line - read-only, no effect on the
    # pool just saved above.
    _log_coverage(pool + extension, log=log)
    # PART 5 STEP 5.2 (8 Oct 2026, Director-directed): the preview
    # cost-estimate log line - advisory only, read-only, unconditional
    # (fires whether or not TOP200_PREVIEW_UNIVERSES is set) - see
    # _log_preview_universe_cost_estimate()'s own docstring.
    _log_preview_universe_cost_estimate(log=log)
    return pool


_SECTOR_FILL_MAX_PER_NIGHT = 40


def _fill_missing_sectors(rows, log=print):
    """Sector fallback Part 2 (27 Sep 2026, owner-reported): a bounded,
    one-shot yfinance fallback for whichever `rows` (pool + extension,
    mutated in place) still have no sector after Part 1's scan-row ->
    sector_cache_store -> ASX static map chain (scanner_engine.sector_
    for_ticker(), called while best_by_ticker is built above) - tickers
    like MELI/CARG/WDFC that have never been scanned under ANY sector-
    carrying universe at all, so no cache/static-map answer exists yet
    either. Capped at _SECTOR_FILL_MAX_PER_NIGHT fetches a night so a
    large blank backlog can never turn one pool selection into a
    hundreds-of-tickers fetch storm; every ticker this learns a sector
    for is written into sector_cache_store (source="top100_info") and
    therefore never fetched again by ANYTHING in this codebase, so the
    steady state (after the first night or two) is zero fetches - only
    a genuinely new, never-before-seen pool entrant costs one more call.

    Reuses nightly_scan._yf_call_with_retry() - the same crumb-
    poisoning/429-retry helper nightly_scan.py's own per-ticker fetch
    loop already uses - rather than writing a second retry loop, and
    nightly_scan.PER_TICKER_SLEEP between calls, same Yahoo-etiquette
    pacing every other per-ticker yfinance loop in this app already
    follows. Never raises, and a fetch failure or a ticker with
    genuinely no sector on Yahoo is simply left blank - this must never
    fail pool selection over a display-only field. Logs
    "[top100] sector fill: N fetched, M learned, K still blank" every
    run (even when N is 0) so the Railway log shows the one-off cost on
    a night with backlog and the steady-state zero afterwards."""
    missing = [r for r in rows if not (r.get("sector") or "").strip()]
    fetched = learned = 0
    for row in missing[:_SECTOR_FILL_MAX_PER_NIGHT]:
        ticker = row["ticker"]
        info = nightly_scan._yf_call_with_retry(
            lambda: yf.Ticker(ticker).info, log, ticker, "sector fill",
        ) or {}
        fetched += 1
        sector = info.get("sector")
        if isinstance(sector, str) and sector.strip():
            sector = sector.strip()
            sector_cache_store.learn(ticker, sector, source="top100_info")
            row["sector"] = sector
            learned += 1
        time.sleep(nightly_scan.PER_TICKER_SLEEP)
    still_blank = sum(1 for r in rows if not (r.get("sector") or "").strip())
    log(f"[top100] sector fill: {fetched} fetched, {learned} learned, {still_blank} still blank")


def pool_changes(current=None, previous=None):
    """{"new": [ticker, ...], "dropped": [{"ticker","reason"}, ...]} -
    the page's own "changes strip". `current`/`previous` default to
    top100_store.current_pool()/previous_pool() when omitted (tests
    pass their own fixtures instead). "reason" for a dropped ticker is
    the best available explanation: outside the pool's Value-Score cut
    (still in some universe's scan, just no longer top 100) vs no
    longer appearing in ANY saved universe scan at all (delisted,
    renamed, or dropped from every index this site tracks)."""
    current = top100_store.current_pool() if current is None else current
    previous = top100_store.previous_pool() if previous is None else previous
    current_tickers = {r["ticker"] for r in current}
    previous_tickers = {r["ticker"] for r in previous}

    new = sorted(current_tickers - previous_tickers)

    current_universe_tickers = set()
    for universe in scan_store.list_saved_universes():
        if universe == IMPORTED_UNIVERSE:
            continue
        payload = scan_store.load_scan_raw(universe)
        if not payload:
            continue
        current_universe_tickers |= {
            (r.get("Ticker") or "").strip().upper() for r in payload.get("rows") or []
        }

    dropped = []
    for ticker in sorted(previous_tickers - current_tickers):
        if ticker in current_universe_tickers:
            reason = "still scanned, but its Value Score fell outside the top 200"
        else:
            reason = "no longer appears in any scanned universe"
        dropped.append({"ticker": ticker, "reason": reason})

    return {"new": new, "dropped": dropped}


# -----------------------------------------------------------------
# AI scoring: dimensions, weights, prompt.
# -----------------------------------------------------------------

# (key, label, weight) - the ten AI-SCORED dimensions (v2 rubric). The
# eleventh composite component, "return", is Python-computed (see
# composite_score() below, publicly labelled "Research Score" on the
# page) and carries WEIGHT_RETURN, not a model-scored dimension - kept
# in this same block purely so every weight lives in ONE place, per
# the task's own "all weights in one constants block for easy tuning"
# instruction.
#
# v2 amendment (Top 100 amendment v2, Sep 2026) replaced v1's 18+10
# scheme (moat/regulatory/balance_sheet_resilience/inflation_exposure,
# WEIGHT_RETURN=18) with this 17+83 one: "moat" -> "competitive
# position" (profitability re-scoring explicitly forbidden - the
# site's own numeric Quality factor already measures it, see the
# DE-CIRCULARISATION PRINCIPLE in _SYSTEM_PROMPT below and this
# commit's own report for the exact Quality-factor input list),
# "regulatory" -> "regulatory & legal" (litigation folded in),
# "balance_sheet_resilience" -> "balance_sheet_fixed_charges"
# (operating fixed-cost rigidity folded in, not just financial
# leverage), "inflation_exposure" DROPPED as a standalone dimension
# (folded into pricing_power, now "pricing power & cost pass-
# through"), and "management" ADDED (new - integrity/candour/
# execution record, distinct from deal outcomes). See RUBRIC_VERSION
# below for why this is a cache-invalidating change, not an in-place
# edit.
WEIGHT_RETURN = 17
DIMENSIONS = [
    ("ai_exposure", "AI exposure", 12),
    ("competitive_position", "Competitive position", 10),
    ("regulatory_legal", "Regulatory & legal", 10),
    ("customer_concentration", "Customer concentration", 10),
    ("pricing_power", "Pricing power & cost pass-through", 10),
    ("accounting_quality", "Accounting quality", 8),
    ("balance_sheet_fixed_charges", "Balance sheet & fixed charges", 8),
    ("management", "Management quality", 6),
    ("capital_allocation", "Capital allocation", 5),
    ("reinvestment_runway", "Reinvestment runway", 4),
]
assert WEIGHT_RETURN + sum(w for _k, _l, w in DIMENSIONS) == 100

DIMENSION_KEYS = [k for k, _l, _w in DIMENSIONS]
DIMENSION_WEIGHT = {k: w for k, _l, w in DIMENSIONS}
DIMENSION_LABEL = {k: l for k, l, _w in DIMENSIONS}

# Top 100 Research Score rework (25 Sep 2026, owner-approved mock): the
# ten AI dimensions' own weights (83 today, since WEIGHT_RETURN=17 and
# the two sum to 100 per the assert above) - the denominator composite_
# score() rescales a fully-scored company's analytical total against,
# and the same number top100_render.py's methodology panel divides by
# to display each dimension's weight rescaled onto a 0-100 feel. A
# derived constant, not a new tunable - DIMENSIONS itself is untouched.
DIMENSION_WEIGHT_TOTAL = sum(w for _k, _l, w in DIMENSIONS)

# >= this many null dimensions (out of ten) -> NOT RATED. The task's
# own rule, named here rather than an inline "3" so it reads the same
# in the prompt text, the scoring parser, and the page.
NOT_RATED_MIN_NULLS = 3

# Cache-key version tag for the SCORING RUBRIC (dimension set, weights,
# anchors, prompt wording - not the Claude model, which already has
# its own cache-key component). Bumped whenever the rubric itself
# changes meaning, so old scores under a retired rubric are never
# silently read back as if they answered the new questions - see
# top100_store.save_score()/get_score()/scores_for_quarter_model()'s
# own rubric_version parameter and _migrate_scores_schema_v2()'s
# migration docstring for the mechanics. "v1" is the implicit,
# unversioned scheme every score was cached under before this constant
# existed - the migration backfills exactly that label for old rows,
# it is never written by new code.
#
# v2 -> v3 (25 Sep 2026, owner-approved mock, "headwind_and_currency_
# risk_mock.html"): current_headwind added to the response schema (see
# _response_schema()'s own comment) - a new question the model answers,
# so every pooled company is "unscored" under v3 until the next nightly
# run re-scores it. This IS the delivery mechanism, not a side effect:
# no schema migration needed (rubric_version was already part of the
# top100_scores PK, exactly for this kind of bump - see
# _migrate_scores_schema_v2()'s own docstring), the current_headwind
# column is a purely additive ALTER TABLE (top100_store.py), and v2
# rows stay preserved under their own key, untouched. Weights,
# DIMENSIONS, max_tokens and the cache-key STRUCTURE are all otherwise
# unchanged by this bump - see composite_score()'s own docstring for
# why a headwind, like the inversion synthesis, has zero effect on any
# score or ranking (display-only, same status as the inversion line).
#
# v3 -> v4 (26 Sep 2026, owner-approved mock, "top100_v4_market_
# structure_onefoot_mock.html"): two more questions added to the
# response schema - market_structure/market_structure_comment (the
# competitive structure of the company's PRIMARY profit pool) and
# one_foot_hurdle/one_foot_comment (Buffett's one-foot-bar test) - see
# _response_schema()'s own comment for the exact fields. Same delivery
# mechanism as v2->v3: every pooled company is "unscored" under v4
# until the next nightly run, the four new columns are a purely
# additive ALTER TABLE (top100_store.py), no rubric_version PK/schema
# migration needed, and v3 (and earlier) rows stay preserved under
# their own key untouched. KNOWN GAP this bump reopens (already
# fixed once, for the v2->v3 bump): every pooled company reads as
# unscored under "v4" until the next nightly run completes, which
# would blank the page/AWAITING-shelve every company for up to a day -
# see top100_render._enriched_pool()'s own "previous rubric" fallback,
# shipped ALONGSIDE this bump specifically so that gap never recurs.
# v4 -> v5 (30 Sep 2026, owner-approved mock, "mock_top100_v5_munger_
# line.html"): two more questions added to the response schema -
# munger_quality/munger_comment (Munger's "great business at a fair
# price" quality test - distinct from one_foot_hurdle, which is about
# the DECISION at today's price, not the business itself) and
# big_wave/big_wave_comment (Munger's "big wave to ride" - is there a
# secular market tailwind carrying the business regardless of its own
# execution) - see _response_schema()'s own comment for the exact
# fields. Same delivery mechanism as v3->v4: every pooled company is
# "unscored" under v5 until the next nightly run, the four new columns
# are a purely additive ALTER TABLE (top100_store.py), no rubric_
# version PK/schema migration needed, and v4 (and earlier) rows stay
# preserved under their own key untouched. The v3->v4 "previous rubric"
# fallback (top100_render._score_row_with_fallback()) already covers
# this transition too - it keys off rubric_version generically, not a
# hardcoded "v4" literal. Weights, DIMENSIONS, max_tokens and the
# cache-key STRUCTURE are all otherwise unchanged - both new question
# pairs are display-only, same zero-effect-on-scoring status as every
# other synthesis field here (see composite_score()'s own docstring -
# it is never touched by this bump).
#
# v5 -> v6 (1 Oct 2026, owner-directed, "shorter rubric + packed
# requests" cost task): NOT a new question - every dimension, label
# vocabulary, sentinel convention and output field is byte-for-byte
# identical to v5. This bump exists because the WIRE SHAPE of the
# request/response changed: up to TOP100_COMPANIES_PER_REQUEST
# companies are now packed into one request (see that constant's own
# comment), the response schema is wrapped in a top-level {"companies":
# [...]} array with a "ticker" field added to each item so results can
# be matched regardless of order (see _response_schema()'s own
# comment), and _SYSTEM_PROMPT itself was trimmed (repeated phrasing +
# the four worked-example "Calibration:" lines cut, every rule/
# criterion/sentinel kept - see _SYSTEM_PROMPT's own comment for the
# exact diff). None of that changes what the model is actually asked
# to judge, but it DOES change what a v5-cached score is an answer to
# (a single-company prompt, not a 5-packed one) - rubric_version is
# already part of the cache key for exactly this kind of "the
# questions are the same but how they were asked changed" bump (same
# reasoning as every prior version here), so this follows the same
# delivery mechanism as v2->v3/v3->v4/v4->v5: every pooled company is
# "unscored" under v6 until the next nightly run (one full re-score,
# ~115 names, budget ~$2.5 under the new packed shape), the previous-
# rubric fallback covers the gap exactly as before, and v5 (and
# earlier) rows stay preserved under their own key untouched.
RUBRIC_VERSION = "v6"

# v6 packed requests (1 Oct 2026, owner-directed): how many companies'
# worth of scoring one Batches API request now carries. Before this,
# every request's own ~10k-token system+schema prefix was paid once PER
# COMPANY; packing N companies into one request still pays that prefix
# only once per REQUEST, so the real saving scales with this constant,
# not with a shorter rubric alone (see _SYSTEM_PROMPT's own trim for
# that separate, smaller saving). Each request's own max_tokens scales
# with however many entrants IT actually carries (see _request_params()
# below) - the last request of a night, when the unscored count doesn't
# divide evenly by this constant, simply packs fewer and gets a
# proportionally smaller budget, never a wasted full-size one.
TOP100_COMPANIES_PER_REQUEST = 5

MODEL_TOP100 = "claude-opus-5-5"

# Anthropic's published per-million-token pricing for this model
# (anthropic.com/claude pricing page, checked Sep 2026) - Batch API
# pricing is 50% of standard, applied in estimate_batch_cost_usd()
# below. Same "hardcoded constant, occasionally rechecked" convention
# ai_client.py's own pricing constants already use, for the same
# reason (no live pricing lookup, one more network call with its own
# failure modes, for a number that changes rarely).
TOP100_INPUT_USD_PER_MTOK = 4.00
TOP100_OUTPUT_USD_PER_MTOK = 20.00

# Honest cost accounting (1 Oct 2026, owner-directed). Root cause of the
# Console-vs-site-log gap this fixes: poll_and_ingest_batch() used to sum
# only usage.input_tokens, which EXCLUDES usage.cache_creation_input_
# tokens/cache_read_input_tokens - separate fields on the same usage
# object. With cache_control still on _request_params()'s system block
# (dropped below, this same push - see that function's own comment) and
# every batch request executing in PARALLEL, most requests miss the
# cache and the ~10k-token system+schema prefix was billed as a cache
# WRITE (1.25x input) on nearly every one of the 108 nightly requests,
# never once counted. Per Anthropic's published pricing: a cache write
# costs 1.25x the standard input rate, a cache read 0.10x - applied in
# estimate_batch_cost_usd() below, alongside the plain input/output
# terms it already had.
CACHE_WRITE_MULTIPLIER = 1.25
CACHE_READ_MULTIPLIER = 0.10
BATCH_DISCOUNT = 0.5

# Pool expansion (1 Oct 2026, owner decision): doubled 120 -> 240
# alongside POOL_SIZE's own 100 -> 200 bump - the per-UTC-day cap of 2
# batches / TOP100_MAX_ENTRANTS_PER_UTC_DAY (= 2 * this constant, see
# below) stays unchanged in SHAPE, so a 215-name expansion night (the
# first night this ships: ~115 names needing a v6 re-score + ~100 new
# expansion entrants) still fits inside one UTC day's two-batch cap.
MAX_NIGHTLY_SCORES = 240

# Results-driven Top 100 refresh (27 Sep 2026, owner-directed): the
# age-based safety net for _unscored_tickers()'s rule (c) - two half-
# year reporting cycles, so a normally-reporting company (US quarterly,
# ASX/LSE half-yearly) always hits rule (b) (its most_recent_quarter
# actually changing) well before it ever reaches this fallback. Only a
# company whose results signal is missing or stuck re-scores on age
# alone.
RESCORE_MAX_AGE_DAYS = 200

# Newcomer persistence filter (1 Oct 2026, owner decision): a ticker
# entering the Top 100 pool for the first time (no score under ANY
# rubric version, and not in the pool the previous night) is not
# submitted until it has appeared in the pool on this many CONSECUTIVE
# nightly selections - see _true_newcomer_gated()'s own docstring.
# Rationale (owner's own words): churn at the pool boundary (positions
# ~90-100) was the main driver of daily submissions; a name that holds
# its place this many nights is far more likely to stay, so the
# ~$0.05-0.10 cost per company is spent once instead of on names that
# fall out the next day. Applies ONLY to never-scored newcomers -
# results-driven re-scores, the RESCORE_MAX_AGE_DAYS safety net,
# rubric-version re-scores (including this v6 bump's own full re-
# score) and the previous-rubric fallback are all unaffected.
NEWCOMER_PERSISTENCE_NIGHTS = 3

# Audit fixes, Commit 1 (30 Sep 2026, owner-directed, "close the no-
# resubmit-loop door"): scheduler_engine._run_top100_poll used to
# resubmit whenever poll_and_ingest_batch() returned non-None and
# nothing was pending - a batch ending 0 scored / N failed still
# returns a dict, so the SAME failed tickers got resubmitted every
# hourly poll, forever, with the failure never recorded anywhere for
# _unscored_tickers() to see. Two independent guards close this:
#
# 1. Per-ticker failure memory (top100_store.top100_score_failures,
#    record_score_failure()/clear_score_failure()/score_failures_for_
#    model()): a ticker whose latest failure under the CURRENT rubric
#    is younger than TOP100_FAILURE_RETRY_HOURS is skipped by
#    _unscored_tickers() until that window passes; once its attempts
#    reach TOP100_FAILURE_MAX_ATTEMPTS it is skipped permanently for
#    this rubric (the row shows "scoring failed - will retry after the
#    next rubric change" on the AWAITING shelf instead of silently
#    retrying forever) - a rubric bump is a new PK row here (rubric_
#    version is part of the key, same convention as top100_scores), so
#    attempts naturally reset to 0 the moment RUBRIC_VERSION changes.
# 2. A hard daily spend cap (top100_store.top100_daily_submissions,
#    record_daily_submission()/get_daily_submission_state(), keyed by
#    UTC date): submit_nightly_batch() refuses beyond either
#    TOP100_MAX_SUBMISSIONS_PER_UTC_DAY batches or TOP100_MAX_ENTRANTS_
#    PER_UTC_DAY entrants for the day - refresh_all()'s force=True path
#    may bypass the BATCH-COUNT cap (an owner-initiated refresh is
#    allowed its own submission even if the automatic nightly job
#    already used its 2) but never the ENTRANT cap (240 = 2 x
#    MAX_NIGHTLY_SCORES - the actual spend ceiling this guard exists
#    to protect).
TOP100_FAILURE_RETRY_HOURS = 24
TOP100_FAILURE_MAX_ATTEMPTS = 3
TOP100_MAX_SUBMISSIONS_PER_UTC_DAY = 2
TOP100_MAX_ENTRANTS_PER_UTC_DAY = 2 * MAX_NIGHTLY_SCORES

# Degenerate-response guard (3 Oct 2026, owner-directed, live evidence:
# msgbatch_01Jbz1uEmi1zesnrEbVHtchE's t100-3/t100-17 - sentinel-filled
# templates, not real judgements, saved as NOT RATED). A degenerate
# item is never saved on its FIRST attempt (recorded as a "degenerate_
# response" failure instead, retried at the next batch like any other
# failure); TOP100_DEGENERATE_ACCEPT_ATTEMPTS (2) names the point at
# which _save_degenerate_as_not_rated() accepts it anyway rather than
# retrying forever - checked as "the STORED failure reason coming into
# this run was already degenerate_response" (poll_and_ingest_batch()'s
# own per-ticker branch), not a raw attempts-count compare, since
# `attempts` itself counts every failure reason, not this one
# specifically.
TOP100_DEGENERATE_ACCEPT_ATTEMPTS = 2

# A blank request is not a strike against its companies (5 Oct 2026,
# owner-directed, live evidence: about one request in three comes back
# wholly blank regardless of which companies are inside it - see
# _whole_response_is_degenerate()'s own docstring for the condition.
# Before this, a whole-blank request fed the SAME "degenerate_response"
# failure reason as a genuine per-item degenerate, so two unlucky blank
# requests could get a perfectly good, well-known company (CL, WDAY,
# PIC.AX on 5 Oct) accepted as NOT RATED by chance. "request_blank" is
# a separate failure reason that NEVER feeds TOP100_DEGENERATE_ACCEPT_
# ATTEMPTS's NOT-RATED path - see poll_and_ingest_batch()'s own branch
# order. It gets more retries than an ordinary failure (this constant,
# not TOP100_FAILURE_MAX_ATTEMPTS) because the fault is almost
# certainly the request, not the company, and a retry after one is
# sent solo (one company per request, see submit_nightly_batch()) to
# test that theory.
TOP100_REQUEST_BLANK_MAX_RETRIES = 5

# Rough pre-submission cost estimate ONLY (the real, billed cost is
# logged after ingest from poll_and_ingest_batch()'s own actual token
# counts - see estimate_batch_cost_usd()'s own docstring). ~4 chars/
# token is a standard English-text rule of thumb; the output-token
# figure is a conservative per-company average from this prompt's own
# shape (ten ~25-word justifications plus the synthesis fields) rather
# than a measured constant - this exists purely so the owner sees an
# order-of-magnitude cost BEFORE a batch goes out, not to be billing-
# accurate.
_CHARS_PER_TOKEN_ESTIMATE = 4
_ESTIMATED_OUTPUT_TOKENS_PER_ENTRANT = 1200


def _estimate_request_tokens(entrants):
    """Honest cost accounting (1 Oct 2026, owner-directed): previously
    summed only the system prompt + user message, silently treating
    the structured-output response SCHEMA (_response_schema(), itself
    roughly as large as the system prompt once every dimension's
    description text is serialized) as free - the other half of why
    the old pre-submission estimate undershot the real billed cost.
    Both are now counted, and _request_params() no longer marks the
    system block cacheable (see that function's own comment) - so
    every one of these tokens is billed as plain input, not a cache
    write/read, on the real request too; this estimate's own caller
    (submit_nightly_batch()) prices it at the plain input rate for
    exactly that reason, no CACHE_WRITE_MULTIPLIER needed here.

    v6 packed requests (1 Oct 2026, owner-directed): `entrants` is now
    one PACKED REQUEST's worth (up to TOP100_COMPANIES_PER_REQUEST
    rows), not one company - the system prompt and schema are counted
    ONCE for the whole group here, exactly as they are on the real
    request, which is the actual saving mechanism of the pack (see
    TOP100_COMPANIES_PER_REQUEST's own comment): submit_nightly_batch()
    calls this once per packed group and sums across groups, rather
    than once per company as it did pre-v6."""
    params = _request_params(entrants)
    system_text = params["system"][0]["text"]
    user_text = params["messages"][0]["content"]
    schema_text = json.dumps(params["output_config"]["format"]["schema"])
    return max(1, (len(system_text) + len(schema_text) + len(user_text)) // _CHARS_PER_TOKEN_ESTIMATE)


_SYSTEM_PROMPT = """You are screening publicly-listed companies for a factual, descriptive "Top 100" quality shortlist on an investing research site. You are given a short list of companies (ticker, name, sector). Score EACH ONE independently, purely on its own facts, on TEN qualitative dimensions, each as an integer from 1 to 5 - 5 is ALWAYS the good outcome for a long-term holder of the stock, 1 is ALWAYS the bad outcome, on every dimension, no exceptions. Your response format has NO null/blank values anywhere - every field below names the exact SENTINEL value that stands in for "no value" wherever one is needed.

For each dimension also give a ONE-LINE justification, AT MOST ABOUT 25 WORDS, naming the specific source period it is based on (e.g. "FY25 annual report", "Q2 2026 investor call", "the company's own FY24 10-K risk factors section") - or an empty string "" for source_period if the dimension's score is 0 (see the honesty rule).

HONESTY RULE, the single most important instruction in this prompt: output 0 for a dimension's score (not a number 1-5, and never a middle value like 3 to "play it safe") for ANY dimension you do not have confident, specific, public-record knowledge of for THIS company. 0 is not a real score - it is the sentinel meaning "cannot score honestly". Guessing a plausible-sounding score is worse than admitting you don't know - a 0 is the honest answer, a fabricated 3 is not. If three or more of your ten scores end up 0, that is expected and correct for a company with a thin public record - do not distort your other scores to avoid it. This applies especially to MANAGEMENT QUALITY (dimension 8 below): score it 0 freely whenever the people running the company aren't publicly well known - most companies have no public record of their management's integrity, candour or execution track record, and that is the honest, expected answer, not a failure. The `ticker` field is an EXACT echo of the ticker you were given for that company - copy it verbatim, never leave it empty and never apply the empty-string sentinel to it, including for a NOT RATED company.

DE-CIRCULARISATION PRINCIPLE - read this before scoring anything: this site separately computes a purely NUMERIC "Quality" factor straight from public financial-statement data (return on equity, net profit margin, return on invested capital, revenue growth, earnings growth, free cash flow sign, and the debt-to-equity ratio). Your job is to add judgment that numeric pipeline CANNOT see - never to restate what it already measures. Concretely, for five of the ten dimensions below:
  - Competitive position: do NOT score whether the company is currently profitable or how profitable it is (the numeric pipeline already has that) - score only the DURABILITY of its edge relative to named peers.
  - Pricing power & cost pass-through: do NOT score today's margin level (already measured) - score the forward-looking ability to raise prices or pass through cost increases without losing volume.
  - Balance sheet & fixed charges: do NOT score the raw debt-to-equity ratio alone (already measured) - score net debt/EBITDA, interest coverage, the maturity wall, and fixed operating-cost rigidity, none of which the numeric pipeline sees.
  - Capital allocation: do NOT score the company's current return-on-invested-capital LEVEL (already measured) - score the quality of its INCREMENTAL capital-deployment decisions: recent M&A returns, and buybacks versus stock-based-compensation dilution.
  - Reinvestment runway: do NOT score the recent growth RATE (already measured) - score how much runway remains for capital to keep compounding at high returns going FORWARD.
The other five dimensions (AI exposure, regulatory & legal, customer concentration, accounting quality, management quality) aren't touched by the numeric pipeline at all - score those fully on their own terms.

THE TEN DIMENSIONS AND THEIR ANCHORS (1 = worst for a holder, 5 = best for a holder):

1. AI EXPOSURE - substitution risk vs strengthening.
   5: immune to AI disruption, or a clear beneficiary - AI increases demand for what it sells, or the company's own use of AI is a structural cost or product advantage.
   1: the core product or service is directly substitutable by an AI tool at a fraction of the cost.

2. COMPETITIVE POSITION - advantage versus 2-3 NAMED direct competitors: the profitability GAP and its DURABILITY (see the de-circularisation principle above - never re-score absolute profitability itself).
   5: structurally more profitable than its named peers for a durable reason (network effects, high switching costs, a regulatory licence, genuine brand/scale pricing power) with clear multi-year evidence it has persisted.
   1: undifferentiated among stronger rivals - no discernible edge, competing purely on price.
   REQUIRED justification format: "vs {named peers}: {comparison}, {durability reason}".

3. REGULATORY & LEGAL - regulation AND litigation exposure together.
   5: regulation is a tailwind that compels purchase of the company's product, or forms a protective moat around it.
   1: an existential political or procurement risk, or a major litigation overhang - a plausible single regulatory/policy change or court outcome could eliminate a large share of revenue.

4. CUSTOMER CONCENTRATION.
   5: thousands of small, individually-replaceable customers; no single customer is material to revenue.
   1: one customer is more than roughly 30% of revenue, or a small handful of customers collectively dominate it.

5. PRICING POWER & COST PASS-THROUGH (absorbs inflation exposure - see the de-circularisation principle above, never re-score today's margin level).
   5: has repeatedly raised prices above inflation without losing meaningful volume, and can pass through cost increases (including inflationary ones) quickly.
   1: a pure price-taker in a commoditised market with no pass-through mechanism - margins erode directly as costs or inflation rise.

6. ACCOUNTING QUALITY (capitalisation rate + cash conversion).
   5: free cash flow conversion of roughly 80-120% of net income, with minimal capitalisation of what are effectively normal operating costs (e.g. R&D, software development) onto the balance sheet.
   1: aggressive capitalisation of operating-like costs materially inflates reported free cash flow; cash conversion sits far below net income.

7. BALANCE SHEET & FIXED CHARGES - financial AND operating rigidity together (see the de-circularisation principle above, never re-score the raw debt-to-equity ratio alone).
   5: net cash or low net debt/EBITDA, high interest coverage, no concentrated near-term refinancing wall, and a flexible (largely variable) cost structure that can flex down in a downturn.
   1: high leverage, thin interest coverage, a concentrated debt maturity wall in the next 1-2 years, and/or a heavy fixed-cost base that cannot flex down when revenue falls.

8. MANAGEMENT QUALITY - the integrity, candour and execution record of the PEOPLE running the company, distinct from the outcome of any one deal. Score 0 freely where the record isn't publicly known (see the honesty rule above) - most companies have no such record.
   5: a long public record of doing what they said they would do - candid in setbacks, disciplined in guidance, no credibility problems.
   1: a credibility problem - a pattern of over-promising, evasive communication, or conduct that has damaged investor trust.

9. CAPITAL ALLOCATION - incremental ROIC on recent major deployments; buybacks vs SBC dilution (see the de-circularisation principle above, never re-score the company's current ROIC level).
   5: a disciplined, accretive incremental-ROIC record on recent major deployments (M&A, capex); buybacks genuinely reduce the share count net of stock-based-compensation dilution; no pattern of value-destroying write-downs.
   1: a pattern of value-destroying acquisitions or repeated impairments, or buybacks that merely offset SBC dilution without shrinking the real share count.

10. REINVESTMENT RUNWAY - can incremental capital still deploy at current returns (see the de-circularisation principle above, never re-score the recent growth rate).
    5: a long runway of high-return reinvestment opportunities still ahead (an expanding or under-penetrated market) at returns well above the cost of capital.
    1: a mature, saturated market with few remaining high-return reinvestment options - excess cash is likely to be misallocated, or simply returned because there is nowhere better to put it.

INVERSION SYNTHESIS - after scoring all ten dimensions, write ONE sentence naming the single most plausible scenario that could seriously damage this company, plus a severity from 1 (minor) to 5 (plausibly breaks the company). This is a separate analytical synthesis, not a dimension score - it has ZERO effect on any of the ten scores above, on this company's ranking, or on its Top-20 eligibility. If three or more of your ten dimension scores are 0 (this company will be marked NOT RATED), output the sentinel values instead - an empty string "" for the inversion scenario and 0 for its severity - do not invent a damaging scenario for a company you don't know well enough to score in the first place.

CURRENT HEADWIND - a separate field from the inversion above, and easy to confuse with it, so read this carefully: the inversion is HYPOTHETICAL (the worst plausible future scenario); the headwind is ACTUAL and PRESENT (why the market is discounting this company right now, as of your knowledge). In at most 40 words, state the actual, present reason the market is discounting this company - the standing headwind (demand, margins, competition, regulation, sentiment), as of your knowledge. This is what IS weighing on the stock, distinct from the inversion's hypothetical worst case. If no clearly identifiable headwind exists, output an empty string "" - never invent one. Same honesty rule as everywhere else in this prompt: a company you don't know a specific, current headwind for gets the empty-string sentinel, not a guessed one. NOT RATED companies (three or more null dimensions) get the empty-string sentinel here too, same as the inversion fields.

MARKET STRUCTURE (RUBRIC_VERSION v4) - classify the competitive structure of this company's PRIMARY PROFIT POOL: the specific market segment that actually generates the bulk of its profit, NOT the broadest possible industry definition (e.g. a payments network's primary profit pool is card-network processing, not "financial services" broadly). Output exactly one of "monopoly", "duopoly", "oligopoly", "competitive". If you don't have confident, specific knowledge of the competitive landscape, output the sentinel empty string "" - never guess a label you can't justify. Then, in at most 25 words, name the actual competitors/players that justify the label (empty string "" if you declined the label itself).

ONE-FOOT HURDLE (RUBRIC_VERSION v4) - Buffett's "one-foot bar" test: would a well-informed investor consider this an EASY, OBVIOUS investment decision that requires no heroic assumptions about the future? Output exactly "yes" only if the case is genuinely easy and obvious; "no" if the thesis depends on hard-to-predict outcomes, however attractively priced the stock may be. If you don't have enough confident knowledge of the company to judge, output the sentinel empty string "" - never guess. Then, in at most 25 words, explain the verdict (empty string "" if you declined the verdict itself).

MUNGER-QUALITY BUSINESS (RUBRIC_VERSION v5) - would Charlie Munger classify this as a great business worth owning for decades at a fair price, judged on the BUSINESS, not today's price: (1) simple enough to understand and predict, (2) a durable competitive advantage that widens rather than erodes, (3) high returns on capital with room to reinvest at similar returns, (4) able, honest management that thinks like owners, (5) no need for heroic assumptions. Output exactly "yes" only if all five hold clearly; "no" if any one clearly fails; the sentinel empty string "" if you cannot judge - never guess. This is distinct from the one-foot hurdle above, which asks whether the investment DECISION at today's price is easy - a Munger-quality business can fail the one-foot hurdle on price, and a cheap stock can pass the hurdle without being Munger-quality. Then, in at most 25 words, name which of the five criteria decided the verdict (empty string "" if you declined the verdict itself).

BIG WAVE TO RIDE (RUBRIC_VERSION v5) - Munger's "big wave to ride": is there a SECULAR, multi-year trend in the company's primary market that carries the business regardless of its own execution? Output exactly "tailwind" for a structural trend (penetration still early, a demographic or regulatory shift, technology adoption) that should keep growing the market for 5+ years; "headwind" when the market or channel is structurally shrinking or being rerouted (substitution, disintermediation, regulation) - not a cyclical dip; "flat" for a mature, stable market with neither. Judge the market, not the company's share of it - a share gainer in a shrinking market is "headwind". The sentinel empty string "" if you cannot judge - never guess. Then, in at most 25 words, name the specific trend, or its absence (empty string "" if you declined the verdict itself)."""


def _user_prompt(entrants):
    """v6 packed requests (1 Oct 2026, owner-directed): one user message
    listing every entrant in this request (up to TOP100_COMPANIES_PER_
    REQUEST, usually fewer for the night's last, partial group) by
    ticker/company name/sector, instead of the old one-company-per-
    message form. `entrants`: [{"ticker", "company_name", "sector"},
    ...] - run_single_test_call() passes a single-item list so the one
    request/response/parse path is shared by every caller, batched or
    not."""
    lines = [
        f"Score EACH of the following {len(entrants)} companies "
        "independently - judge each purely on its own facts, never let "
        "one company's score or justification influence another's. For "
        "every company, give all ten dimension scores, the one-sentence "
        "inversion synthesis (scenario + severity), the current headwind "
        "(at most 40 words, or an empty string), the market structure of "
        "its primary profit pool (plus a short comment naming the "
        "competitors that justify it), the one-foot-hurdle verdict (plus "
        "a short comment explaining it), the Munger-quality verdict "
        "(plus a short comment naming the deciding criterion), and the "
        "big-wave-to-ride verdict (plus a short comment naming the trend "
        "or its absence). Echo each company's own ticker back in your "
        "response so results can be matched regardless of order.\n",
    ]
    for e in entrants:
        ticker = e["ticker"]
        name = e.get("company_name") or ticker
        sector = (e.get("sector") or "").strip() or "unknown sector"
        lines.append(f"- {ticker}: {name} ({sector})")
    return "\n".join(lines)


def _dimension_schema():
    # v3 (25 Sep 2026, owner-reported, root cause confirmed from
    # production logs): every batch request was STILL rejected after
    # the v2 fix, now with "Schemas contains too many parameters with
    # union types (22 parameters with type arrays or anyOf)...limit:
    # 16" - Claude's structured-outputs schema caps how many
    # properties across the WHOLE schema may use a union type (type as
    # an array, or anyOf/oneOf), and v2's ten {"score","source_period"}
    # nullable pairs plus the two top-level inversion fields added up
    # to exactly 22 (10*2 + 2). Every field is now a PLAIN type with a
    # SENTINEL standing in for "no value" instead of a nullable type:
    # score 0 = "cannot score honestly" (the honesty rule's own null
    # case - real scores are always 1-5), source_period "" = no
    # specific source cited. Both sentinels are mapped back to Python
    # None in _parse_response_json() below, so every consumer
    # downstream of that function (composite_score(), the >=3-nulls
    # NOT RATED rule, the page's own rendering) sees exactly the same
    # values it always did - only the WIRE shape changed, never the
    # meaning. _SYSTEM_PROMPT's own honesty-rule wording is updated to
    # name these sentinels explicitly (below).
    return {
        "type": "object",
        "properties": {
            "score": {"type": "integer",
                      "description": "Integer 1-5 (5=best for a holder, 1=worst), or 0 if not confidently known - see the honesty rule. 0 is NOT a real score; it means \"cannot score honestly\"."},
            "justification": {"type": "string"},
            "source_period": {"type": "string",
                               "description": "The specific source period this score is based on (e.g. \"FY25 annual report\"), or an empty string \"\" if score is 0 (no source applies to a score you didn't give)."},
        },
        "required": ["score", "justification", "source_period"],
        "additionalProperties": False,
    }


def _company_item_schema(schema_mode="legacy"):
    """v2: the ten dimension objects plus the inversion synthesis pair
    (inversion_scenario/inversion_severity) at the top level - no more
    "summary" (v1's "strongest dimension + what to check" note), since
    the page now shows the inversion line in its place (Commit 2's own
    "replacing the what to check note" instruction) and all ten
    justifications directly (the tap-expand detail), leaving no reader
    of the response who still needs a separate summary field.

    COMMIT 4 of instruction_top200_unrated_and_blank_replies_combined.md
    (5 Oct 2026, Director-directed, switch TOP200_SCHEMA_MODE, unset by
    default = "legacy"): when schema_mode == "ticker_first", "ticker"'s
    own entry in the `properties` dict below moves to FIRST (inserted
    before the DIMENSION_KEYS loop instead of after it) - this is the
    ONLY difference the two modes produce anywhere in this schema.
    `required`'s own list order is untouched in both modes (it already
    puts "ticker" first, see the return below) - only the `properties`
    dict's own key order, and therefore its JSON serialization order,
    moves. No field's type/description/content changes in either mode.

    v3 (25 Sep 2026): see _dimension_schema()'s own comment - every
    property here is a plain type with a sentinel, zero union types
    anywhere in this schema (the whole point of this rewrite).

    RUBRIC_VERSION v3 (25 Sep 2026, owner-approved mock, "headwind_
    and_currency_risk_mock.html"): added current_headwind, a PLAIN
    string (no union type, no minLength/maxLength - both unsupported
    by structured outputs, same constraint every other field here
    already respects) with the SAME "" -> null sentinel convention as
    inversion_scenario just above - "no clearly identifiable headwind"
    is a permitted, honest answer, never guessed.

    RUBRIC_VERSION v4 (26 Sep 2026, owner-approved mock, "top100_v4_
    market_structure_onefoot_mock.html"): added market_structure/
    market_structure_comment and one_foot_hurdle/one_foot_comment -
    four more PLAIN strings, same "" -> None sentinel convention, same
    zero-union-type/zero-min-max discipline. market_structure and
    one_foot_hurdle each have a small fixed vocabulary of allowed
    values ("monopoly"/"duopoly"/"oligopoly"/"competitive" and
    "yes"/"no" respectively) but are declared as plain "string" here,
    NOT as an enum-constrained/union type - structured outputs doesn't
    support enum validation any more than it supports min/max (same
    constraint that forced the sentinel rewrite in the first place);
    the allowed-value check is enforced server-side in
    _parse_response_json() below instead, exactly like every other
    range/vocabulary constraint in this schema.

    RUBRIC_VERSION v5 (30 Sep 2026, owner-approved mock, "mock_top100_
    v5_munger_line.html"): added munger_quality/munger_comment and
    big_wave/big_wave_comment - four more PLAIN strings, same ""->None
    sentinel convention, same zero-union-type/zero-min-max discipline,
    same small-fixed-vocabulary-enforced-server-side pattern as market_
    structure/one_foot_hurdle just above ("yes"/"no" and "tailwind"/
    "flat"/"headwind" respectively).

    RUBRIC_VERSION v6 (1 Oct 2026, owner-directed, packed requests):
    this is now the PER-COMPANY ITEM schema - _response_schema() below
    wraps an array of these inside a top-level {"companies": [...]}
    object, so up to TOP100_COMPANIES_PER_REQUEST of them travel in one
    request/response. The only field added here for that purpose is
    "ticker" (a plain string, required) so poll_and_ingest_batch() can
    match each item back to the entrant it answers for regardless of
    the order the model returns them in. Every dimension/field below
    this point is otherwise byte-for-byte identical to v5 - diffed by
    hand against the v5 schema as part of this bump, zero wording
    changes."""
    _ticker_prop = {
        "type": "string",
        "description": "The exact ticker of the company this item answers for, copied verbatim from the list in the user message - used to match this result back to its entrant regardless of response order.",
    }
    props = {}
    if schema_mode == "ticker_first":
        props["ticker"] = _ticker_prop
    for key in DIMENSION_KEYS:
        props[key] = _dimension_schema()
    if schema_mode != "ticker_first":
        props["ticker"] = _ticker_prop
    props["inversion_scenario"] = {
        "type": "string",
        "description": "One-sentence inversion scenario, or an empty string \"\" if this company is NOT RATED (see the honesty rule) - never invent a scenario for a company you don't know well enough to score.",
    }
    props["inversion_severity"] = {
        "type": "integer",
        "description": "Severity 1 (minor) to 5 (plausibly breaks the company), or 0 if this company is NOT RATED. 0 is NOT a real severity.",
    }
    props["current_headwind"] = {
        "type": "string",
        "description": "At most 40 words: the actual, present reason the market is discounting this company - distinct from the hypothetical inversion scenario above. An empty string \"\" if no clearly identifiable headwind exists, or if this company is NOT RATED - never invent one.",
    }
    props["market_structure"] = {
        "type": "string",
        "description": "The competitive structure of this company's PRIMARY profit pool - exactly one of \"monopoly\", \"duopoly\", \"oligopoly\", \"competitive\", or an empty string \"\" if you don't have confident, specific knowledge of the competitive landscape. Never guess a label you can't justify.",
    }
    props["market_structure_comment"] = {
        "type": "string",
        "description": "At most 25 words naming the actual competitors/players that justify the market_structure label above. An empty string \"\" if market_structure is \"\".",
    }
    props["one_foot_hurdle"] = {
        "type": "string",
        "description": "Buffett's one-foot-bar test - exactly \"yes\" only if this is an easy, obvious investment decision requiring no heroic assumptions about the future; \"no\" if the thesis depends on hard-to-predict outcomes, however attractive the price. An empty string \"\" if you don't have enough confident knowledge to judge - never guess.",
    }
    props["one_foot_comment"] = {
        "type": "string",
        "description": "At most 25 words explaining the one_foot_hurdle verdict above. An empty string \"\" if one_foot_hurdle is \"\".",
    }
    props["munger_quality"] = {
        "type": "string",
        "description": "Would Charlie Munger classify this as a great business worth owning for decades at a fair price, judged on the BUSINESS, not today's price - simple to understand, a durable widening advantage, high returns on capital with reinvestment room, able and honest owner-minded management, no heroic assumptions needed. Exactly \"yes\" only if all five hold clearly; \"no\" if any one clearly fails; an empty string \"\" if you cannot judge - never guess. Distinct from one_foot_hurdle, which is about the DECISION at today's price, not the business itself.",
    }
    props["munger_comment"] = {
        "type": "string",
        "description": "At most 25 words naming which of the five criteria decided the munger_quality verdict above. An empty string \"\" if munger_quality is \"\".",
    }
    props["big_wave"] = {
        "type": "string",
        "description": "Munger's 'big wave to ride' - is there a SECULAR, multi-year trend in the company's primary market that carries the business regardless of its own execution? Exactly \"tailwind\" for a structural trend still early enough to keep growing the market for 5+ years; \"headwind\" when the market or channel is structurally shrinking or being rerouted, not a cyclical dip; \"flat\" for a mature, stable market with neither. Judge the market, not the company's share of it. An empty string \"\" if you cannot judge - never guess.",
    }
    props["big_wave_comment"] = {
        "type": "string",
        "description": "At most 25 words naming the specific trend behind the big_wave verdict above, or its absence. An empty string \"\" if big_wave is \"\".",
    }
    return {
        "type": "object",
        "properties": props,
        "required": ["ticker"] + DIMENSION_KEYS + [
            "inversion_scenario", "inversion_severity", "current_headwind",
            "market_structure", "market_structure_comment",
            "one_foot_hurdle", "one_foot_comment",
            "munger_quality", "munger_comment",
            "big_wave", "big_wave_comment",
        ],
        "additionalProperties": False,
    }


def _response_schema(schema_mode="legacy"):
    """v6 packed requests (1 Oct 2026, owner-directed): the top-level
    wrapper - a single "companies" array holding one _company_item_
    schema() per entrant in this request. An array-of-objects wrapper
    is NOT a union type (confirmed against the task's own schema dry-
    check rule) - it carries zero union types and no min/max/minLength/
    maxLength/minItems/maxItems anywhere, same zero-union-type/zero-
    min-max discipline the per-item schema itself has kept since v3
    (see _company_item_schema()'s own docstring, formerly this
    function's own docstring before the v6 wrap).

    `schema_mode` (COMMIT 4, 5 Oct 2026) is passed straight through to
    _company_item_schema() - see its own docstring for the only thing
    it changes."""
    return {
        "type": "object",
        "properties": {
            "companies": {
                "type": "array",
                "items": _company_item_schema(schema_mode=schema_mode),
            },
        },
        "required": ["companies"],
        "additionalProperties": False,
    }


def _request_params(entrants, schema_mode="legacy"):
    """The exact MessageCreateParamsNonStreaming-shaped dict for one
    REQUEST - `entrants`: [{"ticker", "company_name", "sector"}, ...],
    up to TOP100_COMPANIES_PER_REQUEST of them (run_single_test_call()
    passes a single-item list) - shared by submit_nightly_batch()
    (wrapped in a Batches Request, one per packed group) and run_
    single_test_call() (sent directly, for the task's own one real API
    test call).

    `schema_mode` (COMMIT 4 of instruction_top200_unrated_and_blank_
    replies_combined.md, 5 Oct 2026) is passed straight through to
    _response_schema() - "legacy" (the default) reproduces today's
    exact request byte-for-byte; "ticker_first" changes only where
    "ticker" sits in the per-company properties object.

    v6 packed requests (1 Oct 2026, owner-directed): max_tokens now
    scales with len(entrants) - 4000 tokens per company (the same per-
    company budget this constant has had since 25 Sep 2026, see its
    own comment below) times however many companies THIS request
    actually carries, so a full TOP100_COMPANIES_PER_REQUEST-sized
    request gets 4000*5=20000 and the night's final, smaller group
    gets proportionally less, never a wasted full-size budget. Well
    under Claude Opus 5.5's own 128k-token standard max-output ceiling
    (platform.claude.com/docs/en/models/opus-5-5/overview, checked 1
    Oct 2026) - no output-300k-2026-03-24 beta header needed.

    Honest cost accounting (1 Oct 2026, owner-directed): the system
    block's own "cache_control": {"type": "ephemeral"} is REMOVED here.
    Verified against the Anthropic Console for 30 Sep 2026: 1,511,091
    total tokens / $4.66 billed, versus 269k tokens / $2.47 this
    module's own (pre-fix) ingest log showed for that day's two
    batches - the gap is the ~10k-token system+schema prefix, sent
    with every one of the 108 requests, counted as a cache WRITE
    (1.25x input) on nearly all of them: the Batches API runs every
    request in PARALLEL, so one request's cache write is rarely still
    warm by the time a sibling request needs to read it - a cache hit
    would need the opposite, sequential execution, to be the common
    case. No usage/cache-hit-ratio history is persisted anywhere in
    this codebase (top100_scores stores the prompt/response TEXT, never
    the usage object - see top100_store.save_score()'s own docstring),
    so there is no evidence this ever paid for itself; dropped rather
    than kept on an unverified hope of a hit. If a future measurement
    (via top100_store.ingest_cost_last_n_days()'s own cache-write/
    cache-read breakdown, now recorded going forward) ever shows reads
    outweighing writes, this is the one line to restore."""
    return {
        "model": MODEL_TOP100,
        # SMALL FIX (25 Sep 2026, owner-reported): was 2000 - 19/100
        # batch results failed with JSON parse errors ("Unterminated
        # string" around char 2500-3400), i.e. the response was cut off
        # by max_tokens mid-object before the JSON could close. Ten
        # dimensions' worth of {score, justification, source_period}
        # plus the two inversion fields, all inside one structured-
        # outputs JSON object, doesn't reliably fit in 2000 output
        # tokens - raised to 4000 (2x) to comfortably fit all ten
        # justifications + the inversion synthesis even before
        # _SYSTEM_PROMPT's own new ~25-word-per-justification cap
        # (added the same day) further reduces the typical case. v6
        # packed requests (1 Oct 2026): now 4000 PER ENTRANT in this
        # request (see this function's own docstring).
        "max_tokens": 4000 * len(entrants),
        "system": [{"type": "text", "text": _SYSTEM_PROMPT}],
        "messages": [{"role": "user", "content": _user_prompt(entrants)}],
        "output_config": {"format": {"type": "json_schema", "schema": _response_schema(schema_mode=schema_mode)}},
    }


def current_quarter(today=None):
    """"2026Q3"-style calendar-quarter label.

    Results-driven Top 100 refresh (27 Sep 2026, owner-directed): this is
    NO LONGER the score cache/cadence key - see _unscored_tickers()/
    _score_key_for_entrant()'s own docstrings for the key that replaced
    it ("RP<date>"/"D<date>", results-event-driven, not calendar-driven).
    Nothing in the scoring-decision path or the render path calls this
    any more. Kept only as a plain label generator - existing fixtures
    that seed a literal "2026Q3"-style legacy row still use it for
    exactly that, and every pre-27-Sep-2026 row in top100_scores really
    does carry one of these as its (now-frozen, never-recomputed) key."""
    d = today or datetime.now(timezone.utc).date()
    return f"{d.year}Q{(d.month - 1) // 3 + 1}"


_ALLOWED_MARKET_STRUCTURES = ("monopoly", "duopoly", "oligopoly", "competitive")
_ALLOWED_ONE_FOOT_HURDLE = ("yes", "no")
_ALLOWED_MUNGER_QUALITY = ("yes", "no")
_ALLOWED_BIG_WAVE = ("tailwind", "flat", "headwind")
# Safety net only - the prompt's own target is ~25 words per comment;
# this just bounds the worst case (a model ignoring that guidance)
# rather than enforcing the target itself.
_COMMENT_MAX_WORDS = 40


def _truncate_words(text, max_words=_COMMENT_MAX_WORDS):
    """Hard word-count truncation - never raises, never fails a result
    over an overlong comment (task's own explicit rule). "" / None
    pass through unchanged."""
    if not text:
        return text
    words = text.split()
    if len(words) <= max_words:
        return text
    return " ".join(words[:max_words])


def _parse_one_company(data):
    """Parses one company's own ALREADY-EXTRACTED item dict (one entry
    of the packed response's "companies" array) into (dims_dict,
    not_rated, inversion_scenario, inversion_severity, current_
    headwind, market_structure, market_structure_comment, one_foot_
    hurdle, one_foot_comment, munger_quality, munger_comment, big_wave,
    big_wave_comment). `dims_dict`: {key: {"score", "justification",
    "source_period"}, ...} for all ten keys. Raises ValueError on a
    missing dimension - the caller (_parse_response_json below) treats
    that as THIS ITEM's own failure only, never the other items in the
    same packed response.

    v6 packed requests (1 Oct 2026, owner-directed): this is the exact
    per-company parsing body RUBRIC_VERSION v2-v5 already had, moved
    unchanged into its own function so _parse_response_json() below
    can apply it to each item of the packed "companies" array
    independently - one malformed company must never cost the other
    (up to) TOP100_COMPANIES_PER_REQUEST-1 companies in the same
    request their own, perfectly good results. The ticker-matching
    itself (reading/validating each item's own "ticker" field) lives
    in _parse_response_json(), not here - this function only ever sees
    the dimension/synthesis fields of one already-identified item.

    munger_quality/big_wave (RUBRIC_VERSION v5, 30 Sep 2026, owner-
    approved mock): same belt-and-braces vocabulary enforcement, same
    label-decided-comment-nulled rule, and same hard-truncation via
    _truncate_words() as market_structure/one_foot_hurdle just below -
    copied exactly, not reinvented. Like those two, neither is a NOT-
    RATED-forced-null field (this box is never rendered for a NOT RATED
    company in the first place, so there is nothing for a stray value
    to leak into) and neither ever reaches composite_score() or any
    sort/selection path - display-only, same status as every other
    synthesis field this function returns.

    munger_quality/big_wave (RUBRIC_VERSION v5, 30 Sep 2026, owner-
    approved mock): same belt-and-braces vocabulary enforcement, same
    label-decided-comment-nulled rule, and same hard-truncation via
    _truncate_words() as market_structure/one_foot_hurdle just below -
    copied exactly, not reinvented. Like those two, neither is a NOT-
    RATED-forced-null field (this box is never rendered for a NOT RATED
    company in the first place, so there is nothing for a stray value
    to leak into) and neither ever reaches composite_score() or any
    sort/selection path - display-only, same status as every other
    synthesis field this function returns.

    market_structure/one_foot_hurdle (RUBRIC_VERSION v4, 26 Sep 2026,
    owner-approved mock): each has a small fixed vocabulary the wire
    schema can't itself enforce (structured outputs supports neither
    enums nor min/max - see _company_item_schema()'s own comment), so it's
    enforced HERE, server-side, the same belt-and-braces spot every
    other range/vocabulary rule in this function already lives: a
    value outside the allowed set is treated as a decline (None), same
    as the model outputting the "" sentinel itself. The matching
    comment is forced to None whenever its own label is None too, even
    if the model didn't null it - a comment justifying a label that
    was just discarded as invalid would be an orphaned artefact, not a
    real answer, so it never survives to be stored or shown. Neither
    field is a NOT-RATED-forced-null like inversion_scenario/
    current_headwind below - unlike those, this never reaches the page
    for a NOT RATED company anyway (top100_render._render_row() is
    never called for one; NOT RATED companies render from the bottom
    shelf, which never shows this box), so there is nothing for a
    stray value to leak into. Comments are also hard-truncated
    (_truncate_words()) as the task's own safety net against an
    overlong response - never a failure, just a trim.

    current_headwind (RUBRIC_VERSION v3, 25 Sep 2026, owner-approved
    mock): the model's one-line, present-tense "why is the market
    discounting this company right now" answer - same "" -> None
    sentinel mapping and same NOT-RATED force-null belt-and-braces
    layer as inversion_scenario/inversion_severity below, added in the
    SAME spot for the SAME reason (never trust the model alone to null
    it for a NOT RATED company).

    Belt-and-braces honesty enforcement (task's own explicit rule,
    "NOT-RATED rows: no inversion line, nothing invented"): a NOT
    RATED result has its inversion fields forced to None here,
    server-side, regardless of what the model actually returned - the
    _SYSTEM_PROMPT already instructs the model to null them itself for
    a NOT RATED company, but this is the second, code-enforced layer
    that can't be defeated by the model simply not following that
    instruction.

    Range enforcement (24 Sep 2026, owner-reported): the structured-
    outputs schema (_dimension_schema()/_company_item_schema() above)
    can no longer declare "1-5" via minimum/maximum - Claude's structured-
    outputs JSON Schema subset doesn't support numerical constraints on
    ANY type - so the 1-5 range is enforced HERE instead, server-side,
    the same "belt and braces, code-enforced, can't be defeated by the
    model not following the prompt" layer as the NOT-RATED inversion
    scrub just above. A dimension score outside 1-5 is treated exactly
    like a null score (honesty-rule violation, not a crash) - it counts
    toward the >=NOT_RATED_MIN_NULLS NOT RATED rule same as a genuine
    null. inversion_severity outside 1-5 is CLAMPED to the nearest
    bound instead (1 or 5) - it's a synthesis severity rating, not a
    per-dimension honesty signal, so an out-of-range value is far more
    likely to be an off-by-one from the model than evidence the whole
    synthesis should be discarded.

    Sentinel mapping (25 Sep 2026, owner-reported, v3 schema): the wire
    schema no longer has nullable fields at all (see _dimension_
    schema()'s own comment - a union-type parameter cap forced this).
    Score 0 and inversion_severity 0 both mean the same thing the old
    None did ("cannot score honestly" / "NOT RATED") and are mapped to
    None right here, in the SAME spot the old out-of-range check
    already lived - every line below this comment, and everything that
    reads dims_dict/not_rated/inversion_* afterward, is unchanged."""
    dims = {}
    null_count = 0
    for key in DIMENSION_KEYS:
        d = data.get(key)
        if not isinstance(d, dict):
            raise ValueError(f"missing dimension {key!r}")
        score = d.get("score")
        if score is not None and not isinstance(score, int):
            score = int(score)
        if score == 0:
            score = None
        elif score is not None and not (1 <= score <= 5):
            score = None
        if score is None:
            null_count += 1
        source_period = d.get("source_period")
        if source_period == "":
            source_period = None
        dims[key] = {
            "score": score,
            "justification": d.get("justification") or "",
            "source_period": source_period,
        }
    not_rated = null_count >= NOT_RATED_MIN_NULLS

    inversion_scenario = data.get("inversion_scenario")
    if inversion_scenario == "":
        inversion_scenario = None
    inversion_severity = data.get("inversion_severity")
    if inversion_severity is not None and not isinstance(inversion_severity, int):
        inversion_severity = int(inversion_severity)
    if inversion_severity == 0:
        inversion_severity = None
    elif inversion_severity is not None:
        inversion_severity = max(1, min(5, inversion_severity))
    current_headwind = data.get("current_headwind")
    if current_headwind == "":
        current_headwind = None

    if not_rated:
        inversion_scenario = None
        inversion_severity = None
        current_headwind = None

    market_structure = data.get("market_structure")
    if isinstance(market_structure, str):
        market_structure = market_structure.strip().lower()
    if market_structure not in _ALLOWED_MARKET_STRUCTURES:
        market_structure = None
    market_structure_comment = data.get("market_structure_comment") or None
    if market_structure is None:
        market_structure_comment = None
    else:
        market_structure_comment = _truncate_words(market_structure_comment)

    one_foot_hurdle = data.get("one_foot_hurdle")
    if isinstance(one_foot_hurdle, str):
        one_foot_hurdle = one_foot_hurdle.strip().lower()
    if one_foot_hurdle not in _ALLOWED_ONE_FOOT_HURDLE:
        one_foot_hurdle = None
    one_foot_comment = data.get("one_foot_comment") or None
    if one_foot_hurdle is None:
        one_foot_comment = None
    else:
        one_foot_comment = _truncate_words(one_foot_comment)

    munger_quality = data.get("munger_quality")
    if isinstance(munger_quality, str):
        munger_quality = munger_quality.strip().lower()
    if munger_quality not in _ALLOWED_MUNGER_QUALITY:
        munger_quality = None
    munger_comment = data.get("munger_comment") or None
    if munger_quality is None:
        munger_comment = None
    else:
        munger_comment = _truncate_words(munger_comment)

    big_wave = data.get("big_wave")
    if isinstance(big_wave, str):
        big_wave = big_wave.strip().lower()
    if big_wave not in _ALLOWED_BIG_WAVE:
        big_wave = None
    big_wave_comment = data.get("big_wave_comment") or None
    if big_wave is None:
        big_wave_comment = None
    else:
        big_wave_comment = _truncate_words(big_wave_comment)

    return (dims, not_rated, inversion_scenario, inversion_severity, current_headwind,
            market_structure, market_structure_comment, one_foot_hurdle, one_foot_comment,
            munger_quality, munger_comment, big_wave, big_wave_comment)


def _normalize_echoed_ticker(raw_ticker, expected_tickers=None):
    """Ticker-echo normalisation (3 Oct 2026, owner-directed, batch
    inspector finding on msgbatch_01Jbz1uEmi1zesnrEbVHtchE): the model
    sometimes echoes a ticker under a different STRING than the one it
    was given - a dash/dot variant ("BRK-B" for "BRK.B"), or the bare
    stem with an exchange suffix dropped ("RG1" for "RG1.AX"). Resolves
    `raw_ticker` (already stripped/uppercased) against `expected_
    tickers` (this request's own entrant list) when given: tries the
    dash<->dot swap first, then a stem match (everything before the
    first "."), but ONLY ever substitutes when EXACTLY ONE expected
    ticker matches - an ambiguous match (more than one candidate, or
    none) is left as the model's own literal string rather than
    guessed. Returns raw_ticker unchanged when expected_tickers isn't
    given, or when raw_ticker already matches one of them exactly."""
    t = raw_ticker.strip().upper()
    if not expected_tickers or t in expected_tickers:
        return t
    variants = {t, t.replace("-", "."), t.replace(".", "-")}
    exact = [e for e in expected_tickers if e in variants]
    if len(exact) == 1:
        return exact[0]
    stem = t.split(".")[0]
    stem_matches = [e for e in expected_tickers if e.split(".")[0] == stem]
    if len(stem_matches) == 1:
        return stem_matches[0]
    return t


_DEGENERATE_FREE_TEXT_FIELDS = (
    "inversion_scenario", "current_headwind", "market_structure_comment",
    "one_foot_comment", "munger_comment", "big_wave_comment",
)


def _is_degenerate_item(item):
    """True when `item` (one raw company object straight off the wire,
    BEFORE _parse_one_company()'s own sentinel-mapping/normalisation)
    is a sentinel-filled TEMPLATE, not an actual judgement (3 Oct 2026,
    owner-directed, live evidence from msgbatch_01Jbz1uEmi1zesnrEbVHtchE:
    t100-3 - 5 items, every one of the ten dimension scores 0, every
    free-text field "" - and t100-17, the identical shape with every
    score 1 instead): (a) all TEN dimension scores are IDENTICAL (all
    0, all 1, ...) AND (b) every free-text field - the ten per-
    dimension justifications, plus inversion_scenario/current_headwind/
    market_structure_comment/one_foot_comment/munger_comment/big_wave_
    comment - is empty.

    A legitimate NOT RATED response (>= 3 zero dimensions, but at
    least one dimension scored differently, or carrying a real
    justification) fails condition (a) or (b) and is NOT degenerate -
    this only catches the exact all-identical/all-blank shape, never a
    genuine decline. A malformed item (missing a dimension key, a
    non-integer score) returns False here too - that is _parse_one_
    company()'s own failure to raise on, not this guard's job."""
    scores = []
    for key in DIMENSION_KEYS:
        dim = item.get(key)
        if not isinstance(dim, dict):
            return False
        try:
            score = int(dim.get("score"))
        except (TypeError, ValueError):
            return False
        scores.append(score)
        if str(dim.get("justification") or "").strip():
            return False
    if len(set(scores)) != 1:
        return False
    for field in _DEGENERATE_FREE_TEXT_FIELDS:
        if str(item.get(field) or "").strip():
            return False
    return True


def _item_free_text_all_empty(item):
    """Same free-text surface _is_degenerate_item() checks (the ten
    per-dimension justifications, plus _DEGENERATE_FREE_TEXT_FIELDS),
    without that function's own "all ten scores identical" requirement.
    Used only by _parse_response_json()'s whole-response-degenerate
    check below (condition B: too few items AND every field blank) -
    never to decide a single item's own fate, which stays
    _is_degenerate_item()'s job alone."""
    for key in DIMENSION_KEYS:
        dim = item.get(key)
        if isinstance(dim, dict) and str(dim.get("justification") or "").strip():
            return False
    for field in _DEGENERATE_FREE_TEXT_FIELDS:
        if str(item.get(field) or "").strip():
            return False
    return True


def _whole_response_is_degenerate(companies, expected_tickers):
    """Commit 1 (4 Oct 2026, owner-directed - "a wholly blank packed
    response fails every company in it the same way"): True when a
    packed request's ENTIRE response is a failure, not just some of
    its own items - live evidence: request t100-0 (batch
    msgbatch_01CZd7EPW99kfK1SNqaFUEfG), 3 entrants (APAM/ASIC/HIPO),
    one blank-shaped item returned. Under the per-item logic alone,
    APAM (the one item present, degenerate-shaped, explicit ticker)
    was correctly recorded "degenerate_response", but ASIC/HIPO (no
    item at all) fell through to "missing_from_response" - two
    different failure reasons for what is really one broken request.

    True under EITHER:
      (a) `companies` is non-empty, every entry is a dict, and every
          one is degenerate per _is_degenerate_item() (the model
          returned only sentinel-filled templates); or
      (b) `companies` has FEWER entries than `expected_tickers` (the
          model stopped early / returned nothing for some entrants)
          AND every entry that IS present is a dict with every free-
          text field blank (_item_free_text_all_empty()) - a looser
          check than (a) since a short response's few real items
          needn't share one identical score to still be worthless
          templates. An empty `companies` list satisfies this
          vacuously (0 items, nothing to violate "every field blank").

    False whenever at least one returned item carries a real
    judgement (a non-empty justification or comment anywhere) - that
    keeps today's per-item behaviour (one bad entrant never costs its
    packmates their own good scores) exactly as before this commit.
    `expected_tickers` is required (no entrant list, no "too few
    items" comparison possible) - always False when it's None."""
    if expected_tickers is None:
        return False
    if companies and all(isinstance(item, dict) for item in companies) \
            and all(_is_degenerate_item(item) for item in companies):
        return True  # condition (a)
    if len(companies) < len(expected_tickers):
        return all(  # condition (b) - vacuously True for an empty list
            isinstance(item, dict) and _item_free_text_all_empty(item)
            for item in companies
        )
    return False


def _log_request_diagnostic(log, custom_id, tickers_in_order, items_returned,
                             stop_reason, output_tokens, blank):
    """A3 (5 Oct 2026, owner-directed, diagnostics only - "make the
    blank replies readable for Andrew and the Director"): ONE log line
    per request, emitted by poll_and_ingest_batch() for EVERY result
    regardless of outcome (succeeded, errored, or any other non-
    succeeded status) - custom id, entrant tickers in order, items
    returned, stop reason, output tokens, blank yes/no. Changes
    nothing about what is sent to the model or how a response is
    parsed/scored - purely an additional, separate log line alongside
    the existing per-ticker/per-batch ones. `log` may be None (no-op)
    for a caller that doesn't want this line."""
    if log is None:
        return
    log(f"[top100] diag request {custom_id}: tickers={tickers_in_order} "
        f"items={items_returned} stop_reason={stop_reason} "
        f"output_tokens={output_tokens} blank={'yes' if blank else 'no'}")


def _parse_response_json(text, expected_tickers=None, log=None, degenerate_out=None,
                          matched_by_out=None, request_label=None,
                          request_blank_out=None, existing_failures=None):
    """v6 packed requests (1 Oct 2026, owner-directed): parses the
    whole packed response text into {ticker: (the same 13-tuple
    _parse_one_company() above returns), ...} - one entry per item in
    the response's top-level "companies" array whose own "ticker"
    field is a non-empty string AND whose dimension/synthesis fields
    parse cleanly via _parse_one_company(). ticker matching is by
    exact string (uppercased/stripped, then _normalize_echoed_ticker()'s
    dash/dot/stem resolution when `expected_tickers` is given) -
    order-independent for a company that DID echo a usable ticker,
    since the model is free to return the packed companies in any
    order.

    Positional fallback (3 Oct 2026, owner-directed, batch inspector
    finding): a company with a BLANK/missing "ticker" field (the model
    applied the honesty rule's "" sentinel convention to the echo
    field itself, mostly on an all-zero NOT RATED company) is matched
    by its POSITION in the response against `expected_tickers`' own
    position - items are returned in the request's own input order, so
    the i-th item answers for the i-th entrant. Only attempted when
    `expected_tickers` is given AND len(companies) == len(expected_
    tickers) - a count mismatch means position no longer reliably maps
    to entrant, so a blank-ticker item is left unmatched rather than
    guessed (the caller then records its entrant as missing, exactly
    as before this fix). Never overwrites an entrant already matched
    by an explicit ticker string. `log`, when given, is called once per
    positional match (for the caller's own log stream) - this function
    stays silent (and side-effect-free beyond that) otherwise.

    Degenerate-response guard (3 Oct 2026, owner-directed): an item
    for which _is_degenerate_item() is True - whether matched by an
    explicit ticker string or by position - is never added to the
    returned dict and is instead recorded into `degenerate_out` (a
    set, when given) under its resolved ticker, so the caller can
    treat it as its own distinct failure reason ("degenerate_response")
    rather than either a saved score or a plain "missing" one.

    Per-item failure isolation (the task's own explicit requirement):
    a single malformed item (not a dict, or a missing dimension inside
    it) is simply left OUT of the returned dict rather than raising -
    it never prevents any other item in the same response from being
    parsed and returned. The caller (poll_and_ingest_batch) is
    responsible for comparing this dict's keys against the request's
    own expected ticker list and recording a per-ticker "missing_from_
    response" failure for anything this request was supposed to answer
    for but didn't.

    Raises ValueError only when the response ITSELF is unparseable
    JSON, or has no top-level "companies" list at all - a REQUEST-
    level failure (the whole structured-output call came back
    malformed), which the caller treats as every entrant THIS request
    carried having failed, not a single ticker's problem.

    `matched_by_out` (stored score viewer, 3 Oct 2026, owner-directed):
    a dict, when given, mutated to record HOW each resolved ticker
    (saved score or degenerate) was matched - "ticker" for an explicit
    echoed ticker string, "position" for the blank-ticker positional
    fallback - so the caller can persist it onto the saved score row
    for the admin Stored score viewer panel. Purely an out-parameter,
    same pattern as `degenerate_out`; never changes this function's
    return shape.

    Whole-response-blank guard (Commit 1, 4 Oct 2026; reclassified to
    its own failure reason, Commit A1, 5 Oct 2026, owner-directed -
    "a blank request is not a strike against its companies"): checked
    BEFORE any per-item parsing, via _whole_response_is_degenerate()
    (see its own docstring) - when the packed response is degenerate
    AS A WHOLE, every one of `expected_tickers` is recorded into
    `request_blank_out` (a SEPARATE set from `degenerate_out` - a
    whole-blank request says nothing about any one entrant, so it must
    never feed TOP100_DEGENERATE_ACCEPT_ATTEMPTS's NOT-RATED path the
    way a genuine per-item degenerate does), not just whichever items
    happened to carry an explicit/positional ticker, and this function
    returns {} immediately, so every entrant of this request gets the
    SAME failure reason ("request_blank") rather than some getting
    "missing_from_response" for what is really one broken response.
    Falls back to `degenerate_out` when `request_blank_out` is not
    given (back-compat for a caller that hasn't adopted the new
    reason). `existing_failures` ({ticker: {"reason", "attempts", ...}},
    from top100_store.score_failures_for_model()), when given, is used
    only to compute the "attempt A/N" figure in the log line below -
    the stored attempts count for a ticker already carrying a
    "request_blank" failure, plus one for this one; a packed (multi-
    entrant) request only ever contains entrants with NO existing
    request_blank failure (such a ticker is retried solo - see
    submit_nightly_batch()'s own packing), so this is 1 for a packed
    request and the stored count + 1 for a solo retry. `request_label`
    (the request's own custom_id, e.g. "t100-0"), when given, names the
    request in the one log line this emits for it. Never fires when
    `expected_tickers` is None (no entrant list to compare against or
    record)."""
    data = json.loads(text)
    companies = data.get("companies")
    if not isinstance(companies, list):
        raise ValueError("response has no top-level \"companies\" array")
    if expected_tickers is not None and _whole_response_is_degenerate(companies, expected_tickers):
        # A1 (5 Oct 2026, owner-directed): "whole response degenerate"
        # is a vacuous concept for a SOLO (one-entrant) request that
        # was never itself a request_blank retry - there, "the whole
        # response is bad" and "this one company's own item is bad"
        # are the exact same fact, which is the pre-existing per-item
        # degenerate guard's job (test_top100_degenerate_guard.py's own
        # ZERO1/ONE1 checks), not this new reason. Routed as request_
        # blank only when either (a) this is a genuinely PACKED request
        # (more than one entrant - packing is the thing being blamed),
        # or (b) at least one entrant is ALREADY mid a request_blank
        # retry chain (a solo retry that comes back blank again must
        # keep accumulating under request_blank, per TOP100_REQUEST_
        # BLANK_MAX_RETRIES - it must never fall back to the two-strike
        # degenerate_response path just because it happens to be solo).
        _route_as_request_blank = request_blank_out is not None and (
            len(expected_tickers) > 1
            or any((existing_failures or {}).get(t, {}).get("reason") == "request_blank"
                   for t in expected_tickers)
        )
        if _route_as_request_blank:
            if request_blank_out is not None:
                request_blank_out.update(expected_tickers)
            if matched_by_out is not None:
                for t in expected_tickers:
                    matched_by_out.setdefault(t, "whole_response")
            if log is not None:
                attempt = 1
                if existing_failures:
                    attempts_seen = [
                        (existing_failures.get(t) or {}).get("attempts", 0) + 1
                        for t in expected_tickers
                        if (existing_failures.get(t) or {}).get("reason") == "request_blank"
                    ]
                    if attempts_seen:
                        attempt = max(attempts_seen)
                log(f"[top100] request {request_label or '?'}: blank "
                    f"({len(companies)} items for {len(expected_tickers)} entrants) - "
                    f"{len(expected_tickers)} recorded as request_blank, "
                    f"attempt {attempt}/{TOP100_REQUEST_BLANK_MAX_RETRIES}")
        else:
            if degenerate_out is not None:
                degenerate_out.update(expected_tickers)
            if matched_by_out is not None:
                for t in expected_tickers:
                    matched_by_out.setdefault(t, "whole_response")
            if log is not None:
                log(f"[top100] request {request_label or '?'}: whole response degenerate "
                    f"({len(companies)} items for {len(expected_tickers)} entrants) - "
                    f"all {len(expected_tickers)} recorded as degenerate_response")
        return {}
    out = {}
    blank_items = []  # [(index, item), ...] - tried again below, positionally
    for idx, item in enumerate(companies):
        if not isinstance(item, dict):
            continue
        ticker = item.get("ticker")
        if not isinstance(ticker, str) or not ticker.strip():
            blank_items.append((idx, item))
            continue
        ticker = _normalize_echoed_ticker(ticker, expected_tickers)
        if _is_degenerate_item(item):
            if degenerate_out is not None:
                degenerate_out.add(ticker)
            if matched_by_out is not None:
                matched_by_out[ticker] = "ticker"
            continue
        try:
            out[ticker] = _parse_one_company(item)
        except Exception:
            continue
        if matched_by_out is not None:
            matched_by_out[ticker] = "ticker"
    count_mismatch = expected_tickers is not None and len(companies) != len(expected_tickers)
    if blank_items and expected_tickers is not None and not count_mismatch:
        for idx, item in blank_items:
            candidate = expected_tickers[idx]
            if candidate in out or (degenerate_out is not None and candidate in degenerate_out):
                continue
            if _is_degenerate_item(item):
                if degenerate_out is not None:
                    degenerate_out.add(candidate)
                if matched_by_out is not None:
                    matched_by_out[candidate] = "position"
                if log is not None:
                    log(f"[top100] {candidate}: degenerate response, blank ticker - "
                        f"matched by position (item {idx + 1} of {len(companies)})")
                continue
            try:
                parsed = _parse_one_company(item)
            except Exception:
                continue
            out[candidate] = parsed
            if matched_by_out is not None:
                matched_by_out[candidate] = "position"
            if log is not None:
                log(f"[top100] {candidate}: ticker field blank in response - "
                    f"matched by position (item {idx + 1} of {len(companies)})")
    return out


# -----------------------------------------------------------------
# Batch submit / poll (two-phase, non-blocking).
# -----------------------------------------------------------------

def _true_newcomer_gated(ticker, model, presence_map):
    """Newcomer persistence filter (1 Oct 2026, owner decision): True
    when `ticker` should be HELD BACK from tonight's "new_or_rubric"
    bucket because it's a genuinely never-scored newcomer that hasn't
    yet held its pool slot for NEWCOMER_PERSISTENCE_NIGHTS consecutive
    nightly selections.

    Applies ONLY to a true newcomer - a ticker that already carries a
    score under ANY OTHER rubric version (top100_store.
    latest_score_previous_rubric() returns non-None) is a rubric-bump
    entrant, not a newcomer, and is NEVER gated here: it has already
    proven it belongs in the pool, and the v6 full re-score (this same
    bump) must not be held back waiting on a presence streak that has
    nothing to do with it. `presence_map` - from top100_store.
    pool_presence_map() - is read once per _unscored_tickers() call and
    passed in, not re-queried per ticker. A ticker absent from
    presence_map (never seen by update_pool_presence()/seed_pool_
    presence() at all) reads as 0 consecutive nights - fully gated,
    same as a ticker on its own very first night."""
    if top100_store.latest_score_previous_rubric(ticker, model, RUBRIC_VERSION) is not None:
        return False
    return presence_map.get(ticker, 0) < NEWCOMER_PERSISTENCE_NIGHTS


def _failure_exhausted(failure):
    """A1 (5 Oct 2026, owner-directed): reason-aware replacement for a
    flat `attempts >= TOP100_FAILURE_MAX_ATTEMPTS` compare - a
    "request_blank" failure (the request itself was blank, not a
    strike against this ticker) gets TOP100_REQUEST_BLANK_MAX_RETRIES
    (5) attempts before it's permanently skipped for this rubric;
    every other reason keeps the existing TOP100_FAILURE_MAX_ATTEMPTS
    (3). `failure` is one score_failures_for_model() entry ({"reason",
    "attempts", ...}), or None (never exhausted - nothing recorded)."""
    if not failure:
        return False
    attempts = failure.get("attempts") or 0
    limit = (TOP100_REQUEST_BLANK_MAX_RETRIES if failure.get("reason") == "request_blank"
             else TOP100_FAILURE_MAX_ATTEMPTS)
    return attempts >= limit


def _unscored_tickers(pool, model):
    """Results-driven Top 100 refresh (27 Sep 2026, owner-directed):
    which pooled rows need an API call tonight, and WHY. Returns
    [(row, reason), ...] - `reason` is one of:
      - "new_or_rubric": no score exists for this ticker under the
        CURRENT RUBRIC_VERSION/model at all (unchanged behaviour: a
        genuinely new pool entrant, OR every pooled ticker right after
        a rubric bump, until the nightly run catches up - the fallback
        render path covers the gap in the meantime, see top100_render.
        _score_row_with_fallback()). Newcomer persistence filter (1 Oct
        2026, owner decision): a TRUE newcomer (no score under ANY
        rubric at all - see _true_newcomer_gated()'s own docstring for
        how that's distinguished from a rubric-bump entrant) is simply
        left OUT of this list entirely while _true_newcomer_gated()
        returns True for it - it never gets a "deferred" reason tuple,
        since this function's own [(row, reason), ...] contract has no
        slot for "not submitting yet" - see submit_nightly_batch()'s
        own _count_persistence_deferred() for how the deferred count
        is reported separately, for logging/the admin panel only.
      - "new_results": the pool row's own most_recent_quarter and the
        latest current-rubric score's stored most_recent_quarter are
        BOTH non-null and DIFFER - the company has published a new
        reported period since it was last read. A null on either side
        is NEVER a trigger here - Yahoo intermittently returns null for
        this field, and a null-to-null or null-to-value transition on
        its own must not cause a rescore (that would be a data blip, not
        a results event); rule "age" below is what actually catches a
        ticker whose results signal is genuinely missing or stuck.
      - "age": the latest current-rubric score is older than
        RESCORE_MAX_AGE_DAYS (200 days - two half-year reporting
        cycles), regardless of most_recent_quarter - the safety net for
        a company that never triggers "new_results" (a missing/garbled
        signal, or one that has genuinely gone quiet). A missing or
        unparseable scored_at on the existing score is treated as
        maximally stale (this reason fires) rather than as "never
        rescore" - the failure mode of treating a bad timestamp as
        "recent" (a company silently never re-read again) is worse than
        the failure mode of rescoring a company slightly early.
    Every pooled row appears at most once, with its FIRST matching
    reason in the (a, b, c) order above - a brand-new entrant is
    reported as "new_or_rubric" even though it would also trivially
    satisfy "age". latest_scores_for_model() (not scores_for_quarter_
    model()) is the read here specifically because it looks past the
    calendar-quarter-era score key entirely and finds each ticker's
    single newest current-rubric row regardless of what string is in
    its `quarter` column.

    Audit fixes, Commit 1 (30 Sep 2026, owner-directed): a ticker that
    otherwise matches (a)/(b)/(c) above is EXCLUDED from the result
    (never resubmitted) when top100_store.score_failures_for_model()
    shows a failure row for it under this exact (model, RUBRIC_
    VERSION) whose attempts have already reached TOP100_FAILURE_MAX_
    ATTEMPTS (permanent skip for this rubric - see top100_render.py's
    own "scoring failed" shelf caption for how that's surfaced), or
    whose failed_at is younger than TOP100_FAILURE_RETRY_HOURS (a
    temporary skip - eligible again once the window passes). This is
    the fix for the resubmit loop: without it, a batch that ended 0
    scored / N failed left every one of those N tickers looking
    exactly as "unscored" as before, so the next hourly poll
    resubmitted the identical batch forever."""
    latest = top100_store.latest_scores_for_model(model, RUBRIC_VERSION)
    failures = top100_store.score_failures_for_model(model, RUBRIC_VERSION)
    presence_map = top100_store.pool_presence_map([row["ticker"] for row in pool])
    now = datetime.now(timezone.utc)

    def _failure_reentry_reason(ticker, row):
        """COMMIT 2 of instruction_top200_unrated_and_blank_replies_
        combined.md (5 Oct 2026, Director-directed, "a failed company
        is never stuck for good") - for an EXHAUSTED failure only:
        "new_results" or "age" if this ticker should become eligible
        again, else None (stays permanently skipped for this rubric,
        exactly as before this commit). Mirrors the score-row new_
        results/age triggers in the main loop below bit for bit, using
        the failure row's own most_recent_quarter/failed_at as the
        baseline - a failure has no score row to read these from (see
        top100_store.record_score_failure()'s own most_recent_quarter
        param). A failure that predates this commit (most_recent_
        quarter NULL) falls straight through to the age check, same as
        a score row with an unknown most_recent_quarter already does."""
        failure = failures.get(ticker)
        if failure is None or not _failure_exhausted(failure):
            return None
        row_mrq = row.get("most_recent_quarter")
        failure_mrq = failure.get("most_recent_quarter")
        if row_mrq and failure_mrq and row_mrq != failure_mrq:
            return "new_results"
        failed_at = failure.get("failed_at")
        age_days = None
        if failed_at:
            try:
                failed_dt = datetime.fromisoformat(failed_at)
                if failed_dt.tzinfo is None:
                    failed_dt = failed_dt.replace(tzinfo=timezone.utc)
                age_days = (now - failed_dt).days
            except Exception:
                age_days = None
        if age_days is None or age_days > RESCORE_MAX_AGE_DAYS:
            return "age"
        return None

    def _failure_blocks(ticker, row):
        failure = failures.get(ticker)
        if failure is None:
            return False
        if _failure_exhausted(failure):
            return _failure_reentry_reason(ticker, row) is None
        failed_at = failure.get("failed_at")
        if not failed_at:
            return False
        try:
            failed_dt = datetime.fromisoformat(failed_at)
            if failed_dt.tzinfo is None:
                failed_dt = failed_dt.replace(tzinfo=timezone.utc)
        except Exception:
            return False
        return (now - failed_dt).total_seconds() < TOP100_FAILURE_RETRY_HOURS * 3600

    out = []
    for row in pool:
        if _failure_blocks(row["ticker"], row):
            continue
        score = latest.get(row["ticker"])
        if score is None:
            # COMMIT 2: a previously-exhausted failure that just passed
            # _failure_blocks() above did so ONLY because _failure_
            # reentry_reason() fired - report that reason (never
            # newcomer-gated: this ticker has already been attempted
            # before, it is by definition not a true newcomer).
            _reentry_reason = _failure_reentry_reason(row["ticker"], row)
            if _reentry_reason is not None:
                out.append((row, _reentry_reason))
                continue
            if _true_newcomer_gated(row["ticker"], model, presence_map):
                continue
            out.append((row, "new_or_rubric"))
            continue
        row_mrq = row.get("most_recent_quarter")
        score_mrq = score.get("most_recent_quarter")
        if row_mrq and score_mrq and row_mrq != score_mrq:
            out.append((row, "new_results"))
            continue
        age_days = None
        scored_at = score.get("scored_at")
        if scored_at:
            try:
                scored_dt = datetime.fromisoformat(scored_at)
                if scored_dt.tzinfo is None:
                    scored_dt = scored_dt.replace(tzinfo=timezone.utc)
                age_days = (now - scored_dt).days
            except Exception:
                age_days = None
        if age_days is None or age_days > RESCORE_MAX_AGE_DAYS:
            out.append((row, "age"))
    return out


def _count_persistence_deferred(pool, model):
    """Newcomer persistence filter (1 Oct 2026, owner decision): how
    many pooled tickers are CURRENTLY true newcomers held back by
    _true_newcomer_gated() - computed independently of _unscored_
    tickers() (which already silently leaves them out of its own
    result - see that function's own docstring) purely for submit_
    nightly_batch()'s own nightly log line and the Admin Dashboard's
    "deferred" count. Deliberately excludes a ticker that's also
    excluded for an unrelated reason (a recent/permanent scoring
    failure) - those are not "deferred by persistence", they're
    excluded for a different, pre-existing reason, so counting them
    here would double up with the failure-retry machinery's own
    "N failed" telemetry."""
    latest = top100_store.latest_scores_for_model(model, RUBRIC_VERSION)
    failures = top100_store.score_failures_for_model(model, RUBRIC_VERSION)
    presence_map = top100_store.pool_presence_map([row["ticker"] for row in pool])
    now = datetime.now(timezone.utc)
    count = 0
    for row in pool:
        ticker = row["ticker"]
        if ticker in failures:
            failure = failures[ticker]
            if _failure_exhausted(failure):
                continue
            failed_at = failure.get("failed_at")
            if failed_at:
                try:
                    failed_dt = datetime.fromisoformat(failed_at)
                    if failed_dt.tzinfo is None:
                        failed_dt = failed_dt.replace(tzinfo=timezone.utc)
                    if (now - failed_dt).total_seconds() < TOP100_FAILURE_RETRY_HOURS * 3600:
                        continue
                except Exception:
                    pass
        if latest.get(ticker) is not None:
            continue
        if _true_newcomer_gated(ticker, model, presence_map):
            count += 1
    return count


def _score_key_for_entrant(row, reason, today=None):
    """Results-driven Top 100 refresh (27 Sep 2026): the score key
    (goes in top100_scores' own `quarter` column) for one entrant being
    submitted tonight, given the reason _unscored_tickers() returned it
    for. "RP<row's most_recent_quarter>" whenever that's known;
    "D<today>" when it isn't. reason == "age" ALWAYS forces "D<today>",
    even when most_recent_quarter is known and unchanged - a rule-(c)
    entrant by definition has an UNCHANGED most_recent_quarter versus
    its existing score, so deriving "RP<mrq>" here would reproduce that
    score's own existing key exactly and upsert-overwrite it, destroying
    the prior row's history for no reason other than it being old.
    "new_or_rubric"/"new_results" entrants never have this collision
    risk by construction (a genuinely new PK either way - a fresh
    ticker/rubric, or a changed most_recent_quarter), so they get the
    natural RP/D derivation."""
    d = today or datetime.now(timezone.utc).date()
    if reason == "age":
        return f"D{d.isoformat()}"
    return _natural_score_key(row, d)


def _natural_score_key(row, today=None):
    """The plain RP<most_recent_quarter>/D<today> derivation with no
    reason-based override - used by the force=True path (refresh_all()'s
    own sole caller, see submit_nightly_batch()'s docstring) and by
    run_single_test_call(), where reproducing (and upsert-overwriting) an
    existing key for an unchanged ticker is the explicitly accepted
    behaviour for an explicit, owner/diagnostic-initiated call - unlike
    the automatic nightly path, which never overwrites a rule-(c) entrant
    (see _score_key_for_entrant() above)."""
    d = today or datetime.now(timezone.utc).date()
    mrq = row.get("most_recent_quarter")
    return f"RP{mrq}" if mrq else f"D{d.isoformat()}"


def _serialize_batch_result_error(result_error):
    """Full serialized payload for one errored batch result's
    result.result.error, truncated to _ERROR_LOG_CHAR_LIMIT chars -
    fixes the blank "error: " log line: that field is an ErrorResponse
    WRAPPER (its own .type is always the literal "error", and it has
    no top-level .message at all) whose INNER .error object carries
    the real Anthropic type/message. Rather than keep picking specific
    attributes (the exact bug here - the previous code picked the
    WRONG level), this logs the whole thing: model_dump_json() first
    (pydantic's own serialization, matches the SDK's real wire shape
    byte for byte), falling back to json.dumps(..., default=str) for
    anything that isn't a pydantic model. Never raises - a serialization
    failure itself becomes the logged string, never a lost result."""
    if result_error is None:
        return "null"
    try:
        return result_error.model_dump_json()[:_ERROR_LOG_CHAR_LIMIT]
    except Exception:
        pass
    try:
        return json.dumps(result_error, default=str)[:_ERROR_LOG_CHAR_LIMIT]
    except Exception:
        pass
    try:
        return repr(result_error)[:_ERROR_LOG_CHAR_LIMIT]
    except Exception:
        return "<unserializable batch result error>"


def _batch_result_error_type(result_error):
    """Best-effort error TYPE for the by-type summary count only (the
    full detail is _serialize_batch_result_error() above) -
    result_error's own INNER .error.type when present (the real
    Anthropic error type, e.g. "invalid_request_error",
    "overloaded_error"), falling back to the outer wrapper's own
    .type ("error", literal - see _serialize_batch_result_error()'s
    own docstring for why that's not the real type) only when the
    inner object is missing entirely."""
    if result_error is None:
        return "unknown"
    inner = getattr(result_error, "error", None)
    inner_type = getattr(inner, "type", None) if inner is not None else None
    return inner_type or getattr(result_error, "type", "unknown")


def _save_degenerate_as_not_rated(ticker, entrant_info, state, prompt_params, raw_text,
                                   matched_by=None, schema_mode=None, log=print):
    """Degenerate-response guard (3 Oct 2026, owner-directed): accepts
    `ticker` as NOT RATED after TOP100_DEGENERATE_ACCEPT_ATTEMPTS (2)
    consecutive degenerate-response attempts, rather than retrying it
    forever. Saves a SYNTHETIC all-null dims dict (never the model's
    own degenerate scores/text, which carry no real judgement
    regardless of which sentinel value - 0s, 1s, ... - they happened
    to repeat) with every synthesis field None and degenerate_
    accepted=True, so top100_render.py's bottom-shelf row can show a
    distinct "degenerate x2" caption rather than looking like an
    ordinary NOT RATED decline. The raw response text is still stored
    (for audit, same as every other save_score() call) even though its
    content wasn't trusted. Clears the ticker's failure row - it's
    been resolved one way or another, not left pending another
    retry."""
    synthetic_dims = {
        key: {"score": None, "justification": "", "source_period": None}
        for key in DIMENSION_KEYS
    }
    top100_store.save_score(
        ticker=ticker, quarter=entrant_info.get("score_key"), model=state["model"],
        rubric_version=RUBRIC_VERSION, dims=synthetic_dims, not_rated=True,
        most_recent_quarter=entrant_info.get("most_recent_quarter"),
        inversion_scenario=None, inversion_severity=None, current_headwind=None,
        market_structure=None, market_structure_comment=None,
        one_foot_hurdle=None, one_foot_comment=None,
        munger_quality=None, munger_comment=None, big_wave=None, big_wave_comment=None,
        degenerate_accepted=True, matched_by=matched_by,
        prompt=json.dumps(prompt_params), raw_response=raw_text,
        schema_mode=schema_mode,
    )
    top100_store.clear_score_failure(ticker, state["model"], RUBRIC_VERSION)
    log(f"[top100] {ticker}: degenerate response x2 - accepted as NOT RATED (flagged degenerate x2)")


def poll_and_ingest_batch(log=print):
    """Phase 1 of every nightly run: if a batch is in flight (top100_
    store.get_batch_state() is not None), checks its status.
      - "ended": parses every result via _parse_response_json(), saves
        each ticker's score (top100_store.save_score()), clears the
        batch-state row, logs a cost-telemetry line (real token counts
        from the batch results, summed), and returns a summary dict.
      - anything else (still processing): logs and returns None -
        untouched, tries again next night.
      - the retrieve() call itself raising, or the batch itself having
        errored/expired: logged, batch-state cleared (so a fresh batch
        can be submitted next run), NO score is touched either way -
        "any failure leaves prior scores intact", the task's own
        guardrail.

    Results-driven Top 100 refresh (27 Sep 2026): each result is saved
    under its OWN per-entrant score key/most_recent_quarter (computed by
    submit_nightly_batch() at submission time, carried through custom_
    id_map - see that function's own docstring for why key computation
    happens there and not here), never one batch-wide `state["quarter"]`
    value for every ticker. custom_id_map entries from a batch submitted
    BEFORE this change shipped are a flat {custom_id: ticker} string map
    with no per-entrant key - handled defensively below by falling back
    to state["quarter"] as that legacy batch's one shared key, so a
    batch already in flight across this deploy still ingests correctly
    instead of crashing.

    v6 packed requests (1 Oct 2026, owner-directed): one Batches API
    RESULT now corresponds to one REQUEST carrying up to TOP100_
    COMPANIES_PER_REQUEST entrants, not one entrant - custom_id_map's
    own per-custom_id value is now {"entrants": {ticker: {"score_key",
    "most_recent_quarter"}, ...}} (a dict of up to 5 tickers), not a
    single ticker's info. A result's own usage (input/output/cache
    tokens) is counted ONCE per result here, exactly as it's billed -
    this is the real saving the pack exists for. _parse_response_json()
    returns {ticker: (13-tuple), ...} for whichever of this request's
    entrants parsed cleanly; each of THIS request's own expected
    tickers that isn't a key of that dict (a genuinely missing item, OR
    the whole request-level parse failing) gets its own "missing_from_
    response"/"parse_error" failure recorded - one bad company, or one
    malformed item, never costs the other (up to) 4 entrants in the
    same request their own perfectly good scores. A batch already in
    flight across this deploy, submitted under the pre-v6 single-
    entrant custom_id_map shape, is still handled (the "ticker" dict
    branch below) - though no Top 100 batch was in flight when this
    bump shipped (confirmed via the scheduler's own idle heartbeat), so
    this is a defensive fallback, not a tested live transition.

    Audit fixes, Commit 1 (30 Sep 2026, owner-directed): every FAILED
    result (errored, non-"succeeded" status, or a JSON parse failure)
    now records a top100_store.record_score_failure() row for its
    ticker with a short reason string, and every SUCCEEDED result
    clears any prior failure row for its ticker (top100_store.clear_
    score_failure()) - a ticker that fails once and later succeeds
    isn't left permanently shadowed by a stale failure count. This is
    what lets _unscored_tickers() stop re-selecting a ticker that just
    failed (the resubmit-loop fix - see that function's own docstring).
    When the batch ends with scored == 0 (nothing at all succeeded),
    this logs "0 scored, N failed - NOT resubmitting" with a short
    sample of the actual failure reasons, so scheduler_engine._run_
    top100_poll's own "only resubmit if scored > 0" check (see that
    function's own docstring) has a clear log trail explaining why it
    didn't fire.

    Returns None if there was nothing to poll, or a summary dict
    {"saved"/"scored", "failed", "input_tokens", "cache_creation_
    tokens", "cache_read_tokens", "output_tokens", "cost_usd"} -
    "saved" is kept alongside "scored" (same value) so no existing
    caller of this function's return dict breaks. The ingest line is
    also persisted via top100_store.record_ingest_cost() so the Admin
    Dashboard's own last-7-days cost panel has something to read."""
    state = top100_store.get_batch_state()
    if state is None:
        return None
    try:
        import anthropic
        client = anthropic.Anthropic()
        batch = client.messages.batches.retrieve(state["batch_id"])
    except Exception as e:
        log(f"[top100] batch status check failed, will retry next run: {e}")
        return None

    if batch.processing_status != "ended":
        log(f"[top100] batch {state['batch_id']} still {batch.processing_status} - checking again next run")
        return None

    custom_id_map = state["custom_id_map"]
    saved, failed = 0, 0
    total_input_tokens, total_output_tokens = 0, 0
    total_cache_creation_tokens, total_cache_read_tokens = 0, 0
    errored_count = 0
    errored_rest_type_counts = {}
    failure_reasons = []  # [(ticker, reason), ...] - audit fixes Commit 1
    # A1 (5 Oct 2026, owner-directed): per-batch request/blank tally for
    # the new aggregate log line below - R counts every request this
    # batch carried (whatever its own outcome), B/blank_request_ids
    # only the ones _parse_response_json() found wholly blank.
    total_requests = 0
    blank_request_ids = []
    # Degenerate-response guard (3 Oct 2026, owner-directed): read ONCE,
    # before the per-result loop, so each degenerate ticker can be
    # checked against whatever it already recorded BEFORE this run -
    # "the SECOND consecutive degenerate attempt" means the stored
    # reason was already "degenerate_response" coming into this run,
    # not merely "this ticker has failed twice for any reason".
    _existing_failures = top100_store.score_failures_for_model(state["model"], RUBRIC_VERSION)
    try:
        for result in client.messages.batches.results(state["batch_id"]):
            entry = custom_id_map.get(result.custom_id)
            if not entry:
                continue
            # v6 packed requests (1 Oct 2026): entrants_map is always
            # {ticker: {"score_key", "most_recent_quarter"}, ...} for
            # whichever ticker(s) THIS request carried - one entry for
            # the common case, up to TOP100_COMPANIES_PER_REQUEST.
            if isinstance(entry, dict) and "entrants" in entry:
                entrants_map = entry["entrants"]
            elif isinstance(entry, dict) and "ticker" in entry:
                # Legacy in-flight batch, pre-v6 single-entrant shape -
                # see this function's own docstring above.
                entrants_map = {entry["ticker"]: {
                    "score_key": entry.get("score_key"),
                    "most_recent_quarter": entry.get("most_recent_quarter"),
                }}
            elif isinstance(entry, str) and entry:
                # Even older legacy in-flight batch (pre-27 Sep 2026) -
                # a flat {custom_id: ticker} string map.
                entrants_map = {entry: {"score_key": state.get("quarter"), "most_recent_quarter": None}}
            else:
                continue
            # COMMIT 4 of instruction_top200_unrated_and_blank_replies_
            # combined.md (5 Oct 2026, Director-directed): read THIS
            # pack's schema_mode back from custom_id_map exactly as
            # submit_nightly_batch() stored it - never re-read fresh
            # from the env var here, since the switch could have
            # changed between submission and ingest (see top200_
            # schema_mode()'s own docstring). Missing entirely (any
            # batch submitted before this commit shipped) reads as
            # "legacy", same convention as top100_store.schema_mode_
            # for_batch().
            pack_schema_mode = entry.get("schema_mode", "legacy") if isinstance(entry, dict) else "legacy"
            if not entrants_map:
                continue
            total_requests += 1
            if result.result.type == "errored":
                # Commit 1 (24 Sep 2026, owner-reported): the real
                # Anthropic error was never logged before this - only
                # "errored, skipped", which made tonight's 100%-errored
                # batch unexplainable from the logs. Commit 1 follow-up
                # (same day): that first fix itself logged a BLANK
                # error ("error: ") - result.result.error is an
                # ErrorResponse wrapper, not the error itself; see
                # _serialize_batch_result_error()'s own docstring. Now
                # logs the FULL serialized error (truncated) for the
                # first _ERRORED_DETAIL_LIMIT results, then one summary
                # line for the rest, counted by (inner) error type. v6
                # packed requests: the WHOLE request errored, so every
                # one of its own entrants is a failure, not just one.
                err = getattr(result.result, "error", None)
                err_type = _batch_result_error_type(err)
                for ticker in entrants_map:
                    failed += 1
                    errored_count += 1
                    failure_reasons.append((ticker, err_type, entrants_map[ticker].get("most_recent_quarter"), pack_schema_mode))
                    if errored_count <= _ERRORED_DETAIL_LIMIT:
                        detail = _serialize_batch_result_error(err)
                        log(f"[top100] {ticker}: batch result errored #{errored_count} - {detail}")
                    else:
                        errored_rest_type_counts[err_type] = errored_rest_type_counts.get(err_type, 0) + 1
                _log_request_diagnostic(log, result.custom_id, list(entrants_map.keys()),
                                         items_returned=0, stop_reason=None,
                                         output_tokens=None, blank=False)
                continue
            if result.result.type != "succeeded":
                for ticker in entrants_map:
                    failed += 1
                    failure_reasons.append((ticker, result.result.type, entrants_map[ticker].get("most_recent_quarter"), pack_schema_mode))
                    log(f"[top100] {ticker}: batch result {result.result.type}, skipped")
                _log_request_diagnostic(log, result.custom_id, list(entrants_map.keys()),
                                         items_returned=0, stop_reason=None,
                                         output_tokens=None, blank=False)
                continue
            msg = result.result.message
            text = next((b.text for b in msg.content if b.type == "text"), "")
            # v6 packed requests: usage is counted ONCE per RESULT
            # (i.e. once per up-to-5-company request) regardless of how
            # many of its own entrants actually parsed below - this is
            # exactly how it's billed, and the real saving mechanism of
            # the pack (see TOP100_COMPANIES_PER_REQUEST's own comment).
            total_input_tokens += getattr(msg.usage, "input_tokens", 0) or 0
            total_output_tokens += getattr(msg.usage, "output_tokens", 0) or 0
            # Honest cost accounting (1 Oct 2026, owner-directed): these
            # two were never read before this - see CACHE_WRITE_
            # MULTIPLIER's own module-level comment for the root cause
            # this fixes.
            total_cache_creation_tokens += getattr(msg.usage, "cache_creation_input_tokens", 0) or 0
            total_cache_read_tokens += getattr(msg.usage, "cache_read_input_tokens", 0) or 0
            _degenerate_tickers = set()
            _request_blank_tickers = set()
            _matched_by = {}
            try:
                parsed_by_ticker = _parse_response_json(
                    text, expected_tickers=list(entrants_map.keys()), log=log,
                    degenerate_out=_degenerate_tickers, matched_by_out=_matched_by,
                    request_label=result.custom_id,
                    request_blank_out=_request_blank_tickers,
                    existing_failures=_existing_failures)
            except Exception as e:
                # Request-level parse failure (malformed/missing
                # "companies" array) - every entrant THIS request
                # carried failed, not just one.
                for ticker in entrants_map:
                    failed += 1
                    failure_reasons.append((ticker, f"parse_error: {e}", entrants_map[ticker].get("most_recent_quarter"), pack_schema_mode))
                log(f"[top100] batch result for {sorted(entrants_map)}: could not parse, skipped ({e})")
                continue
            if _request_blank_tickers:
                blank_request_ids.append(result.custom_id)
            try:
                _raw_companies = json.loads(text).get("companies")
            except Exception:
                _raw_companies = None
            _log_request_diagnostic(
                log, result.custom_id, list(entrants_map.keys()),
                items_returned=len(_raw_companies) if isinstance(_raw_companies, list) else 0,
                stop_reason=getattr(msg, "stop_reason", None),
                output_tokens=getattr(msg.usage, "output_tokens", None),
                blank=bool(_request_blank_tickers) or (
                    isinstance(_raw_companies, list)
                    and _whole_response_is_degenerate(_raw_companies, list(entrants_map.keys()))
                ),
            )
            prompt_params = _request_params([
                {"ticker": t, "company_name": t, "sector": None} for t in entrants_map
            ], schema_mode=pack_schema_mode)
            for ticker, entrant_info in entrants_map.items():
                if ticker in _request_blank_tickers:
                    # A1 (5 Oct 2026, owner-directed): a whole-blank
                    # request is never a strike against the companies
                    # inside it - always recorded as its own "request_
                    # blank" failure reason, NEVER fed to _save_
                    # degenerate_as_not_rated() regardless of how many
                    # consecutive request_blank attempts this ticker
                    # already carries (that NOT-RATED path is
                    # TOP100_DEGENERATE_ACCEPT_ATTEMPTS's alone, for the
                    # genuine per-item degenerate case below). _unscored_
                    # tickers()'s own _failure_exhausted() helper is what
                    # eventually stops retrying after TOP100_REQUEST_
                    # BLANK_MAX_RETRIES - this function only records the
                    # failure, it never stops retrying by itself.
                    failed += 1
                    failure_reasons.append((ticker, "request_blank", entrant_info.get("most_recent_quarter"), pack_schema_mode))
                    continue
                if ticker in _degenerate_tickers:
                    # Degenerate-response guard (3 Oct 2026, owner-
                    # directed): a sentinel-filled template (all ten
                    # dimension scores identical, every free-text field
                    # empty), not a real judgement - never saved as a
                    # score. Accepted as NOT RATED only on the SECOND
                    # consecutive degenerate attempt (the stored failure
                    # reason was already "degenerate_response" coming
                    # into this run); otherwise recorded as a failure
                    # and retried next batch, same retry window as every
                    # other failure reason.
                    _prior = _existing_failures.get(ticker)
                    if _prior and _prior.get("reason") == "degenerate_response":
                        _save_degenerate_as_not_rated(
                            ticker, entrant_info, state, prompt_params, text,
                            matched_by=_matched_by.get(ticker), schema_mode=pack_schema_mode, log=log)
                        saved += 1
                    else:
                        failed += 1
                        failure_reasons.append((ticker, "degenerate_response", entrant_info.get("most_recent_quarter"), pack_schema_mode))
                        log(f"[top100] {ticker}: degenerate response (sentinel-filled template) - "
                            "not saved, will retry")
                    continue
                if ticker not in parsed_by_ticker:
                    # Per-ticker failure isolation (the task's own
                    # explicit requirement): this one entrant's own item
                    # was missing from (or malformed within) the packed
                    # response - the other entrants in the same request,
                    # already in parsed_by_ticker, are saved below exactly
                    # as if nothing had gone wrong.
                    failed += 1
                    failure_reasons.append((ticker, "missing_from_response", entrant_info.get("most_recent_quarter"), pack_schema_mode))
                    log(f"[top100] {ticker}: missing from packed batch response, skipped")
                    continue
                (dims, not_rated, inversion_scenario, inversion_severity, current_headwind,
                 market_structure, market_structure_comment,
                 one_foot_hurdle, one_foot_comment,
                 munger_quality, munger_comment, big_wave, big_wave_comment) = parsed_by_ticker[ticker]
                top100_store.save_score(
                    ticker=ticker, quarter=entrant_info.get("score_key"), model=state["model"],
                    rubric_version=RUBRIC_VERSION, dims=dims, not_rated=not_rated,
                    most_recent_quarter=entrant_info.get("most_recent_quarter"),
                    inversion_scenario=inversion_scenario, inversion_severity=inversion_severity,
                    current_headwind=current_headwind,
                    market_structure=market_structure, market_structure_comment=market_structure_comment,
                    one_foot_hurdle=one_foot_hurdle, one_foot_comment=one_foot_comment,
                    munger_quality=munger_quality, munger_comment=munger_comment,
                    big_wave=big_wave, big_wave_comment=big_wave_comment,
                    matched_by=_matched_by.get(ticker),
                    prompt=json.dumps(prompt_params), raw_response=text,
                    schema_mode=pack_schema_mode,
                )
                top100_store.clear_score_failure(ticker, state["model"], RUBRIC_VERSION)
                saved += 1
    except Exception as e:
        # Audit fix A1 / Fable finding T1 (10 Oct 2026): a stream
        # exception (e.g. a dropped connection partway through
        # client.messages.batches.results()) must be treated like a
        # retrieve() failure - log and return, never fall through to
        # record_score_failure()/clear_batch_state() below. Falling
        # through would wrongly mark whatever tickers happened to be
        # mid-loop as failed, AND discard the in-flight batch's own
        # custom_id_map entirely, leaving nothing for a re-poll to
        # retry for every entrant the stream never reached - while any
        # ticker that WAS fully parsed before the interruption already
        # had its real score saved above and is untouched by this
        # early return.
        log(f"[top100] batch result retrieval failed partway through: {e}")
        return None

    for ticker, reason, mrq, _sm in failure_reasons:
        top100_store.record_score_failure(ticker, state["model"], RUBRIC_VERSION, reason, most_recent_quarter=mrq, schema_mode=_sm)

    if errored_rest_type_counts:
        breakdown = ", ".join(f"{t}: {c}" for t, c in sorted(errored_rest_type_counts.items()))
        log(
            f"[top100] {sum(errored_rest_type_counts.values())} more errored result(s) "
            f"beyond the first {_ERRORED_DETAIL_LIMIT} shown above, by error type - {breakdown}"
        )

    top100_store.clear_batch_state()
    cost = estimate_batch_cost_usd(
        total_input_tokens, total_output_tokens,
        total_cache_creation_tokens, total_cache_read_tokens,
    )
    log(f"[top100] batch {state['batch_id']} ingested: {saved} scored, {failed} failed - "
        f"in {total_input_tokens:,} / cache-write {total_cache_creation_tokens:,} / "
        f"cache-read {total_cache_read_tokens:,} / out {total_output_tokens:,} tokens, "
        f"est. ${cost:.4f} (batch-priced)")
    log(f"[top100] batch {state['batch_id']}: {total_requests} requests, "
        f"{len(blank_request_ids)} blank (ids {', '.join(blank_request_ids)}), "
        f"{saved} scored, {failed} failed")
    # COMMIT 4 of instruction_top200_unrated_and_blank_replies_combined.md
    # (5 Oct 2026, Director-directed): schema_mode + packed/solo split
    # for THIS batch, derived straight from custom_id_map (not from the
    # per-result loop above, so it's unaffected by any result that
    # errored or was skipped before reaching that loop's own counters).
    _schema_mode_counts = {}
    _packed_packs, _solo_packs = 0, 0
    for _entry in custom_id_map.values():
        if not isinstance(_entry, dict):
            continue
        _sm = _entry.get("schema_mode", "legacy")
        _schema_mode_counts[_sm] = _schema_mode_counts.get(_sm, 0) + 1
        _pack_entrants = _entry.get("entrants") or {}
        if len(_pack_entrants) == 1:
            _solo_packs += 1
        elif len(_pack_entrants) > 1:
            _packed_packs += 1
    log(f"[top100] batch {state['batch_id']}: schema_mode "
        f"{', '.join(f'{m}: {c}' for m, c in sorted(_schema_mode_counts.items()))} "
        f"({_packed_packs} packed request(s), {_solo_packs} solo request(s))")
    try:
        top100_store.record_ingest_cost(
            batch_id=state["batch_id"], scored=saved, failed=failed,
            input_tokens=total_input_tokens, cache_creation_tokens=total_cache_creation_tokens,
            cache_read_tokens=total_cache_read_tokens, output_tokens=total_output_tokens,
            cost_usd=cost, custom_id_map=custom_id_map,
        )
    except Exception as e:
        log(f"[top100] could not record ingest cost log: {e}")
    if saved == 0 and failed > 0:
        # Audit fixes, Commit 1 (30 Sep 2026, owner-directed): the
        # resubmit-loop fix's own log line - _run_top100_poll (scheduler_
        # engine.py) reads "saved"/"scored" == 0 from this function's
        # return value to decide NOT to resubmit; this line explains why,
        # right where the ingest summary above it already is.
        sample = ", ".join(f"{t}: {r}" for t, r, _mrq, _sm in failure_reasons[:3])
        log(f"[top100] batch {state['batch_id']}: 0 scored, {failed} failed - "
            f"NOT resubmitting (reason sample: {sample})")
    # COMMIT 1 of instruction_top200_unrated_and_blank_replies_combined.md
    # (5 Oct 2026, Director-directed): "after every ingest" half of the
    # task's own coverage log line, plus the zeros breakdown (ingest-
    # only, since dimension zero-counts only change when scores change).
    # Against the CURRENT pool+extension (not just tonight's entrants) -
    # the task's own "for the current selection" framing.
    _coverage_rows = top100_store.current_pool() + top100_store.current_asx_extension()
    _log_coverage(_coverage_rows, log=log)
    _log_zeros_breakdown(_coverage_rows, log=log)
    # COMMIT 5 of instruction_top200_unrated_and_blank_replies_combined.md
    # (5 Oct 2026, Director-directed): the dry run's own one log line,
    # "after each ingest" - see _log_partial_rating_dry_run()'s own
    # docstring. Read-only, no write, never raises.
    _log_partial_rating_dry_run(log=log)
    return {"saved": saved, "scored": saved, "failed": failed,
            "input_tokens": total_input_tokens, "output_tokens": total_output_tokens,
            "cache_creation_tokens": total_cache_creation_tokens,
            "cache_read_tokens": total_cache_read_tokens,
            "cost_usd": cost}


# -----------------------------------------------------------------
# Resubmission pause (3 Oct 2026, owner-directed, resubmission-loop
# investigation): the 2 Oct 23:00 UTC run packed 215 companies into 43
# requests and came back with 146 scored / 69 "missing from packed
# batch response" - 146 ~ 29*5 suggests whole REQUESTS (not individual
# tickers) came back empty, a pattern _unscored_tickers()'s own 24h/3-
# attempt failure memory (TOP100_FAILURE_RETRY_HOURS/_MAX_ATTEMPTS)
# doesn't fully protect against: those 69 tickers have only ONE failure
# attempt recorded, so a nightly run roughly 24h later would become
# eligible to resubmit them again automatically, before the root cause
# is understood. This is a SEPARATE, explicit, admin-togglable hold -
# a marker file on the same Railway volume every other one-off/
# singleton marker in this module already lives on (see _batch_01xa_
# diagnostic_marker_path()/_pool_expansion_v200_marker_path() just
# below for the identical pattern) - rather than an env var, so the
# owner can flip it from the Admin Dashboard without a redeploy.
# -----------------------------------------------------------------

def _resubmit_pause_marker_path():
    base = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH") or os.path.dirname(__file__)
    return os.path.join(base, ".top100_resubmit_paused")


def is_resubmit_paused():
    """Default OFF (no marker file) - see set_resubmit_paused()."""
    return os.path.exists(_resubmit_pause_marker_path())


def _resubmit_pause_v1_set_marker_path():
    base = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH") or os.path.dirname(__file__)
    return os.path.join(base, ".top100_resubmit_pause_v1_set_done")


def set_resubmit_pause_on_this_deploy_once(log=print):
    """One-off, marker-guarded boot action (3 Oct 2026, owner-directed) -
    same "never allowed to stop the site serving" pattern every other
    one-off cleanup in this module already uses (see diagnose_batch_
    01xa_once()/seed_pool_presence_for_v6_once() just below). Owner's
    own explicit instruction: "SET IT ON in this commit's deploy (via
    the marker, not an env var)" - this is the ONE time the pause marker
    gets written automatically rather than by an owner click. Guarded by
    its OWN marker (distinct from is_resubmit_paused()'s own marker) so
    it fires exactly once, on the first boot after THIS deploy, and
    never re-enables the pause on a later boot even after the owner has
    since turned it back off via the Admin Dashboard toggle."""
    marker = _resubmit_pause_v1_set_marker_path()
    if os.path.exists(marker):
        return
    set_resubmit_paused(True, log=log)
    try:
        with open(marker, "w") as f:
            f.write(datetime.now(timezone.utc).isoformat())
    except OSError as e:
        log(f"[top100] could not write resubmit-pause-v1-set marker: {e}")


def set_resubmit_paused(paused, log=print):
    """Admin Dashboard toggle. Writing the marker (paused=True) holds
    back ONLY the resubmission case described in submit_nightly_batch()
    - newcomers and results-driven re-scores (_unscored_tickers()'s own
    "new_results"/"age" reasons that AREN'T also shadowed by a failure
    record) are never affected, regardless of this flag."""
    marker = _resubmit_pause_marker_path()
    try:
        if paused:
            with open(marker, "w") as f:
                f.write(datetime.now(timezone.utc).isoformat())
        else:
            if os.path.exists(marker):
                os.remove(marker)
    except OSError as e:
        log(f"[top100] could not {'set' if paused else 'clear'} resubmission pause marker: {e}")
        raise
    log(f"[top100] resubmission pause {'ENABLED' if paused else 'disabled'} by owner")


def _resubmit_pause_v2_release_marker_path():
    base = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH") or os.path.dirname(__file__)
    return os.path.join(base, ".top100_resubmit_pause_v2_release_done")


def release_resubmit_pause_on_this_deploy_once(log=print):
    """One-off, marker-guarded boot action (3 Oct 2026, owner-directed,
    degenerate-response guard task item 6) - mirrors set_resubmit_
    pause_on_this_deploy_once() above, in reverse: that hook turned the
    pause ON for the ticker-echo-fix deploy so no bad retries went out
    while the parsing fix landed; the degenerate-response guard +
    retroactive sweep in THIS deploy is the owner-approved release
    point, so this hook turns the pause back OFF automatically - the
    owner doesn't have to touch the Admin Dashboard toggle to let
    tonight's cleared/unmatched tickers go back out.

    Guarded by its OWN marker (distinct from both is_resubmit_paused()'s
    own marker and set_resubmit_pause_on_this_deploy_once()'s "v1"
    marker) so it fires exactly once, on the first boot after THIS
    deploy, and is NEVER re-applied on a later boot - if the owner
    re-pauses resubmission later for their own reasons, this hook must
    never silently clear that pause again."""
    marker = _resubmit_pause_v2_release_marker_path()
    if os.path.exists(marker):
        return
    set_resubmit_paused(False, log=log)
    log("[top100] resubmission pause: OFF (owner-approved release of retries)")
    try:
        with open(marker, "w") as f:
            f.write(datetime.now(timezone.utc).isoformat())
    except OSError as e:
        log(f"[top100] could not write resubmit-pause-v2-release marker: {e}")


# -----------------------------------------------------------------
# Degenerate-response sweep (3 Oct 2026, owner-directed) - retroactive,
# one-off, marker-guarded clean-up of v6 rows already saved from a
# sentinel-filled template (see _is_degenerate_item()'s own docstring
# for the live evidence) - CL/WDAY/DUOL/RRL.AX are expected among the
# tickers it clears. Same "one file per one-off, never allowed to stop
# the site serving" convention as every other boot-time one-off in
# this module (set_resubmit_pause_on_this_deploy_once()/seed_pool_
# presence_for_v6_once() elsewhere here) - fires once, ever, never
# again on a later deploy (unlike the resubmit-pause marker, this is a
# one-time data clean-up, not a per-deploy policy switch).
# -----------------------------------------------------------------

def _is_degenerate_stored_row(score_row):
    """Same test as _is_degenerate_item() (see that function's own
    docstring), reapplied to an ALREADY-STORED score_row (top100_
    store.get_score()/latest_scores_for_model()'s own shape - `dims`
    a dict, score already sentinel-mapped to None for the wire's 0) -
    for the retroactive sweep, which has no raw wire item to re-parse,
    only what was actually persisted."""
    dims = score_row.get("dims") or {}
    if set(dims.keys()) != set(DIMENSION_KEYS):
        return False
    scores = [(dims.get(k) or {}).get("score") for k in DIMENSION_KEYS]
    if len(set(scores)) != 1:
        return False
    if any(str((dims.get(k) or {}).get("justification") or "").strip() for k in DIMENSION_KEYS):
        return False
    for field in _DEGENERATE_FREE_TEXT_FIELDS:
        if str(score_row.get(field) or "").strip():
            return False
    return True


def _degenerate_sweep_marker_path():
    base = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH") or os.path.dirname(__file__)
    return os.path.join(base, ".top100_degenerate_sweep_v1_done")


def run_degenerate_sweep_once(model=MODEL_TOP100, log=print):
    """One-off, marker-guarded boot action: scans every stored v6 score
    row (top100_store.latest_scores_for_model()) for the degenerate
    pattern (_is_degenerate_stored_row()) and, for each match, deletes
    the row (top100_store.delete_score()) and records a "degenerate_
    response" failure for it (top100_store.record_score_failure()) so
    it's picked up and re-sent by _unscored_tickers() in the next
    batch, alongside the 69 unmatched tickers from the same incident.
    Idempotent by construction - run twice with nothing new matching
    (either because the first run already cleared everything, or
    because the marker already short-circuits it), logs "found 0
    matching rows" rather than re-clearing anything. Never raises -
    any failure here is logged and otherwise swallowed, same as every
    other one-off boot hook in this module (this must never be allowed
    to stop the site serving)."""
    marker = _degenerate_sweep_marker_path()
    if os.path.exists(marker):
        return
    cleared = []
    try:
        rows = top100_store.latest_scores_for_model(model, RUBRIC_VERSION)
        for ticker, row in rows.items():
            if _is_degenerate_stored_row(row):
                top100_store.delete_score(ticker, row["quarter"], model, RUBRIC_VERSION)
                top100_store.record_score_failure(
                    ticker, model, RUBRIC_VERSION, "degenerate_response",
                    most_recent_quarter=row.get("most_recent_quarter"))
                cleared.append(ticker)
    except Exception as e:
        log(f"[top100] degenerate sweep failed, will not retry automatically: {e}")
        return
    try:
        with open(marker, "w") as f:
            f.write(datetime.now(timezone.utc).isoformat())
    except OSError as e:
        log(f"[top100] could not write degenerate-sweep marker: {e}")
    if cleared:
        log(f"[top100] degenerate sweep: cleared {len(cleared)} v6 rows: {', '.join(sorted(cleared))}")
    else:
        log("[top100] degenerate sweep: found 0 matching v6 rows")


def _request_blank_strike_conversion_marker_path():
    base = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH") or os.path.dirname(__file__)
    return os.path.join(base, ".top100_request_blank_strike_conversion_v1_done")


def run_request_blank_strike_conversion_once(model=MODEL_TOP100, log=print):
    """One-off, marker-guarded boot action (A1, 5 Oct 2026, owner-
    directed): converts every CURRENTLY-STORED "degenerate_response"
    failure row under (model, RUBRIC_VERSION) to the new "request_
    blank" reason (top100_store.convert_score_failure_reason()), so a
    ticker that already carries one such strike from a whole-blank
    request (the ~23 from 4 Oct) cannot be pushed to NOT RATED by a
    second blank request tonight - TOP100_DEGENERATE_ACCEPT_ATTEMPTS's
    own NOT-RATED path only ever fires for the "degenerate_response"
    reason now (see poll_and_ingest_batch()'s own branch order), so
    converting the stored reason is what actually defuses it.

    Identification method (see this commit's own report for the full
    disclosure): top100_store's score_failures table has no per-
    request/matched_by metadata - it cannot tell a strike that came
    from a genuine per-item degenerate response apart from one that
    came from a whole-blank request by ticker name alone. This sweep
    is therefore BLANKET, not surgical: every stored "degenerate_
    response" row is converted, not just the ~23 from 4 Oct. This is
    safe in only one direction - it can only ever grant a ticker MORE
    retries before any acceptance decision, never fewer, and never
    turns a ticker that would NOT have been accepted into one that
    would - so a row that happened to be a genuine per-item degenerate
    strike simply gets one extra retry cycle under its new reason
    rather than being accepted on its very next consecutive failure,
    which is strictly more lenient, never less safe, for the owner's
    own stated risk preference in this instruction's evidence section.

    Idempotent by construction (same pattern as run_degenerate_sweep_
    once() right above) - run twice with nothing left to convert, logs
    "found 0 matching rows". Never raises - any failure here is logged
    and otherwise swallowed, same as every other one-off boot hook in
    this module."""
    marker = _request_blank_strike_conversion_marker_path()
    if os.path.exists(marker):
        return
    converted = []
    try:
        converted = top100_store.convert_score_failure_reason(
            model, RUBRIC_VERSION, "degenerate_response", "request_blank")
    except Exception as e:
        log(f"[top100] request_blank strike conversion failed, will not retry automatically: {e}")
        return
    try:
        with open(marker, "w") as f:
            f.write(datetime.now(timezone.utc).isoformat())
    except OSError as e:
        log(f"[top100] could not write request-blank-strike-conversion marker: {e}")
    if converted:
        log(f"[top100] request_blank strike conversion: converted {len(converted)} "
            f"degenerate_response row(s) to request_blank: {', '.join(sorted(converted))}")
    else:
        log("[top100] request_blank strike conversion: found 0 matching rows")


def submit_nightly_batch(pool=None, model=MODEL_TOP100, log=print, force=False):
    """Phase 2 of every nightly run, called only when poll_and_ingest_
    batch() found nothing in flight (never both submit AND have a
    batch pending - one in-flight batch at a time, top100_store's own
    singleton row) - and, as of audit fixes Commit 2, also self-checks
    top100_store.get_batch_state() right at the top of this function
    (see that check's own comment below) so a caller that reaches here
    WITHOUT going through poll_and_ingest_batch() first - refresh_all()
    - can't bypass the guarantee either. Selects up to MAX_NIGHTLY_SCORES entrants that need
    scoring from `pool` (defaults to top100_store.current_pool() +
    current_asx_extension()), submits ONE Batches API request covering
    all of them, and persists the batch id + custom_id->entrant map.
    Returns None if there was nothing to submit, or the submitted
    batch's id.

    Results-driven Top 100 refresh (27 Sep 2026, owner-directed): this
    used to select entrants by "unscored for the current calendar
    quarter" and save every result under that one shared `quarter`
    string. Both are gone. Entrants now come from _unscored_tickers(),
    which returns [(row, reason), ...] where reason in ("new_or_rubric",
    "new_results", "age") - see that function's own docstring for the
    exact rule. Each entrant gets its OWN score key, computed HERE at
    submission time (via _score_key_for_entrant(), reason-aware - see
    its own docstring for why rule "age" must NEVER reuse the entrant's
    existing key) rather than at ingest time in poll_and_ingest_batch():
    the a/b/c reason is only available here, right after _unscored_
    tickers() computed it: poll_and_ingest_batch() runs later, possibly
    a full night later, against whatever the POOL looks like THEN, which
    may no longer match the reason this entrant was actually submitted
    for. The score key + most_recent_quarter travel from here to save
    time via custom_id_map (now {custom_id: {"ticker", "score_key",
    "most_recent_quarter"}} - see save_batch_state()'s own docstring for
    why its `quarter` column is now label-only), applied verbatim by
    poll_and_ingest_batch() when it calls save_score() - never
    recomputed there. Logs the reason-mix breakdown BEFORE submitting,
    every night (skipped for force=True - see below - since "every
    pooled company" has no a/b/c breakdown to report):
    "[top100] N to score tonight — new/rubric: a, new results: b,
    age>200d: c".

    force=True (SMALL FIX, 25 Sep 2026, owner-reported, extended 27 Sep
    2026 - refresh_all()'s own sole caller): submits the WHOLE pool
    regardless of whether a current-rubric score already exists,
    skipping the _unscored_tickers() filter entirely. Each entrant still
    gets its own score key, but via the NATURAL derivation only
    (_natural_score_key() - "RP<most_recent_quarter>"/"D<today>", no
    rule-(c)-style forcing) - an unchanged ticker naturally reproduces
    its existing key and upsert-overwrites it, which is the explicitly
    accepted behaviour for an EXPLICIT owner-initiated refresh (unlike
    the automatic nightly path, which must never silently overwrite a
    rule-(c) entrant's history - see _score_key_for_entrant()'s own
    docstring). save_score()'s own per-ticker UPSERT already only ever
    touches a ticker that actually SUCCEEDED in the batch (a failed/
    errored per-ticker result is simply never written - see poll_and_
    ingest_batch()'s own per-result branches), so this loses no failure-
    safety versus the old suffix-key trick this function's git history
    once used for the same purpose - that protection came from save_
    score()'s own gating, not from a separate cache key.

    ASX extension (Top 20 Australia guaranteed-twenty, 25 Sep 2026):
    when `pool` isn't explicitly passed, the default now ALSO includes
    top100_store.current_asx_extension() - so an ordinary nightly run
    (run_nightly() calls this with no `pool` arg) scores extension
    members exactly like pool members, same rubric/cache-key rules, no
    special-casing anywhere below this line. A caller that passes its
    own `pool` (refresh_all() does) controls this explicitly instead.

    Audit fixes, Commit 1 (30 Sep 2026, owner-directed): a hard daily
    spend cap, checked right after `entrants` is known (so a batch that
    would push the day over either limit is refused before any request
    is built or sent) - TOP100_MAX_SUBMISSIONS_PER_UTC_DAY batches and
    TOP100_MAX_ENTRANTS_PER_UTC_DAY entrants, tracked in top100_store's
    top100_daily_submissions table, keyed by UTC date (so the cap
    resets cleanly at day rollover with no extra bookkeeping). force=
    True (refresh_all()'s owner button) bypasses the BATCH-COUNT check
    only - the ENTRANT check still applies even to a forced refresh,
    since that's the actual spend ceiling this guard exists to
    protect, not a "how many times a night" throttle. A rough pre-
    submission cost estimate (see _estimate_request_tokens()'s own
    docstring for why it's an estimate, not the billed figure) is
    logged right before the real submit call, regardless of force.

    v6 packed requests (1 Oct 2026, owner-directed): tonight's
    entrants are grouped into packs of up to TOP100_COMPANIES_PER_
    REQUEST before any request is built - see the packing block's own
    comment below for exactly how. custom_id_map's own per-custom_id
    value is now {"entrants": {ticker: {"score_key", "most_recent_
    quarter"}, ...}}, one dict per PACK rather than per company (see
    poll_and_ingest_batch()'s own docstring for how that's read back).

    Resubmission pause (3 Oct 2026, owner-directed): when is_resubmit_
    paused() is True, any entrant whose ONLY reason to appear in
    _unscored_tickers()'s own output is a prior failure now eligible to
    retry is held back here, before packing - see is_resubmit_paused()'s
    own module-level comment for why. force=True is unaffected (an
    explicit owner-initiated refresh)."""
    # Audit fixes, Commit 2 (30 Sep 2026, owner-directed, tonight_sequence_
    # v2 addendum): the "never both submit AND have a batch pending"
    # guarantee described above only holds when every caller reaches this
    # function through poll_and_ingest_batch()'s own "nothing in flight"
    # check - refresh_all() (the owner's "Refresh all" button) calls this
    # directly and never went through that check, so a Refresh-all fired
    # while the 23:00 UTC nightly's own batch was still in_progress could
    # double-submit: the first batch's custom_id_map gets overwritten by
    # save_batch_state()'s own singleton-row UPSERT, orphaning it - paid
    # for, but never ingested, since poll_and_ingest_batch() only ever
    # tracks the ONE row top100_batch_state holds. Checked here, inside
    # this function itself, so no caller (present or future) can bypass
    # it by skipping poll_and_ingest_batch() - force=True is NOT exempt:
    # the whole point is that a forced refresh is exactly the case that
    # was slipping past the old caller-side-only guarantee.
    in_flight = top100_store.get_batch_state()
    if in_flight is not None:
        log(f"[top100] batch {in_flight['batch_id']} still in progress - not submitting another")
        return None
    pool = (top100_store.current_pool() + top100_store.current_asx_extension()) if pool is None else pool
    if not pool:
        return None
    today = datetime.now(timezone.utc).date()
    if force:
        entrants = [(row, "forced") for row in pool][:MAX_NIGHTLY_SCORES]
    else:
        entrants = _unscored_tickers(pool, model)
        # Resubmission pause (3 Oct 2026, owner-directed) - see
        # is_resubmit_paused()'s own module-level comment. Holds back
        # ONLY an entrant whose reason is "new_or_rubric" (no score at
        # all under the current rubric) AND which also has a failure
        # row on file - i.e. its ONLY reason for appearing here is a
        # prior attempt that failed and is now eligible to retry
        # (either the 24h window passed, or it hasn't hit 3 attempts
        # yet - see _unscored_tickers()/_failure_blocks() above). A
        # genuinely brand-new entrant (no failure row at all - e.g.
        # every pooled ticker right after a rubric bump) is NEVER held
        # back by this, and neither is "new_results"/"age" (those have
        # a real existing score driving the resubmission, not a bare
        # retry).
        if is_resubmit_paused():
            failures = top100_store.score_failures_for_model(model, RUBRIC_VERSION)
            held = [
                (row, reason) for row, reason in entrants
                if reason == "new_or_rubric" and failures.get(row["ticker"]) is not None
            ]
            if held:
                held_tickers = {row["ticker"] for row, _ in held}
                entrants = [e for e in entrants if e[0]["ticker"] not in held_tickers]
                log(f"[top100] resubmission paused by owner: {len(held)} held "
                    f"({', '.join(sorted(held_tickers)[:10])}"
                    f"{', ...' if len(held_tickers) > 10 else ''})")
        entrants = entrants[:MAX_NIGHTLY_SCORES]
        # COMMIT 2 of instruction_top200_unrated_and_blank_replies_
        # combined.md (5 Oct 2026, Director-directed, "a failed company
        # is never stuck for good") - attempts reset the moment a
        # previously-exhausted failure becomes eligible again (an entry
        # in `entrants` can only exist for an exhausted ticker if
        # _unscored_tickers()'s own _failure_reentry_reason() just
        # fired for it - see that function's own docstring). Without
        # this, the very next failure would push attempts straight back
        # past the ticker's own exhaustion ceiling, re-exhausting it
        # immediately and defeating the whole point of re-entry.
        _failures_for_reset = top100_store.score_failures_for_model(model, RUBRIC_VERSION)
        _reentered_tickers = sorted(
            row["ticker"] for row, _reason in entrants
            if _failures_for_reset.get(row["ticker"]) is not None
            and _failure_exhausted(_failures_for_reset[row["ticker"]])
        )
        for _ticker in _reentered_tickers:
            top100_store.clear_score_failure(_ticker, model, RUBRIC_VERSION)
        if _reentered_tickers:
            log(f"[top100] re-entry: {len(_reentered_tickers)} previously exhausted "
                f"ticker(s) eligible again, attempts reset: {', '.join(_reentered_tickers)}")
    if not entrants:
        log(f"[top100] every pooled company already scored under the current rubric for {model} "
            "- nothing to submit")
        return None

    daily = top100_store.get_daily_submission_state(today.isoformat())
    batches_over_cap = (not force) and daily["batches"] >= TOP100_MAX_SUBMISSIONS_PER_UTC_DAY
    entrants_over_cap = (daily["entrants"] + len(entrants)) > TOP100_MAX_ENTRANTS_PER_UTC_DAY
    if batches_over_cap or entrants_over_cap:
        log(f"[top100] daily submission cap reached ({daily['batches']}/{TOP100_MAX_SUBMISSIONS_PER_UTC_DAY} "
            f"batches, {daily['entrants']}/{TOP100_MAX_ENTRANTS_PER_UTC_DAY} entrants) - skipping")
        return None

    if not force:
        counts = {"new_or_rubric": 0, "new_results": 0, "age": 0}
        for _, reason in entrants:
            counts[reason] += 1
        # Newcomer persistence filter (1 Oct 2026, owner decision): the
        # deferred count is computed separately from `entrants` itself -
        # _unscored_tickers() already left these tickers out entirely
        # (see its own docstring), so this re-derives just the count,
        # for the log line and the Admin Dashboard panel only.
        deferred = _count_persistence_deferred(pool, model)
        log(f"[top100] {len(entrants)} to score tonight (re-score {counts['new_results'] + counts['age']}, "
            f"newcomers {counts['new_or_rubric']}, deferred (persistence) {deferred})")

    # v6 packed requests (1 Oct 2026, owner-directed): group tonight's
    # entrants into packs of up to TOP100_COMPANIES_PER_REQUEST - one
    # Batches API Request per pack, not per company (the actual saving
    # mechanism - see that constant's own comment). The night's final
    # pack is simply smaller when len(entrants) doesn't divide evenly -
    # never padded with a dummy entrant, never dropped.
    #
    # A1 solo retry (5 Oct 2026, owner-directed): an entrant whose
    # CURRENT stored failure reason is "request_blank" is packed ALONE
    # (its own one-company request), never grouped with any other
    # entrant - this isolates the retry from whatever made the earlier
    # packed request come back blank, and tells us whether a single-
    # company request also comes back blank. Every other entrant is
    # packed exactly as before this commit.
    _failures_for_packing = top100_store.score_failures_for_model(model, RUBRIC_VERSION)
    _solo_tickers = {
        row["ticker"] for row, _reason in entrants
        if (_failures_for_packing.get(row["ticker"]) or {}).get("reason") == "request_blank"
    }
    solo_entrants = [e for e in entrants if e[0]["ticker"] in _solo_tickers]
    normal_entrants = [e for e in entrants if e[0]["ticker"] not in _solo_tickers]
    packs = [[e] for e in solo_entrants] + [
        normal_entrants[i:i + TOP100_COMPANIES_PER_REQUEST]
        for i in range(0, len(normal_entrants), TOP100_COMPANIES_PER_REQUEST)
    ]
    n_solo_packs = len(solo_entrants)
    n_packed_packs = len(packs) - n_solo_packs

    # COMMIT 4 of instruction_top200_unrated_and_blank_replies_combined.md
    # (5 Oct 2026, Director-directed): read the switch ONCE for this
    # whole submission (not per-pack) - see top200_schema_mode()'s own
    # docstring for why it must not be re-read at ingest time.
    schema_mode = top200_schema_mode(log=log)

    est_input_tokens = sum(
        _estimate_request_tokens([
            {"ticker": row["ticker"], "company_name": row.get("company_name"), "sector": row.get("sector")}
            for row, _reason in pack
        ])
        for pack in packs
    )
    est_output_tokens = len(entrants) * _ESTIMATED_OUTPUT_TOKENS_PER_ENTRANT
    est_cost = estimate_batch_cost_usd(est_input_tokens, est_output_tokens)
    log(f"[top100] estimated cost for tonight's {len(entrants)}-company batch "
        f"({len(packs)} packed request{'s' if len(packs) != 1 else ''}): ${est_cost:.4f} "
        f"(batch-priced, rough pre-submission estimate - real cost logged after ingest)")

    try:
        import anthropic
        from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
        from anthropic.types.messages.batch_create_params import Request
    except ImportError:
        log("[top100] anthropic package not available - skipping tonight's batch")
        return None

    custom_id_map = {}
    requests = []
    for i, pack in enumerate(packs):
        custom_id = f"t100-{i}"
        pack_entrants_meta = {}
        pack_request_entrants = []
        for row, reason in pack:
            ticker = row["ticker"]
            score_key = _natural_score_key(row, today) if force else _score_key_for_entrant(row, reason, today)
            pack_entrants_meta[ticker] = {
                "score_key": score_key,
                "most_recent_quarter": row.get("most_recent_quarter"),
            }
            pack_request_entrants.append({
                "ticker": ticker, "company_name": row.get("company_name"), "sector": row.get("sector"),
            })
        custom_id_map[custom_id] = {"entrants": pack_entrants_meta, "schema_mode": schema_mode}
        requests.append(Request(
            custom_id=custom_id,
            params=MessageCreateParamsNonStreaming(
                **_request_params(pack_request_entrants, schema_mode=schema_mode)),
        ))

    try:
        client = anthropic.Anthropic()
        batch = client.messages.batches.create(requests=requests)
    except Exception as e:
        log(f"[top100] batch submission failed: {e}")
        return None

    top100_store.save_batch_state(batch.id, today.isoformat(), model, custom_id_map)
    top100_store.record_daily_submission(today.isoformat(), len(entrants))
    log(f"[top100] submitted batch {batch.id}: {len(entrants)} compan{'y' if len(entrants) == 1 else 'ies'} "
        f"in {len(packs)} packed request{'s' if len(packs) != 1 else ''} "
        f"({n_packed_packs} packed, {n_solo_packs} solo) for {model}, schema_mode={schema_mode}")
    return batch.id


_BATCH_01XA_DIAGNOSTIC_TARGET = "msgbatch_01XA46gE4evNBqLnZdy2EACR"


def _batch_01xa_diagnostic_marker_path():
    # v2 (24 Sep 2026, owner-reported): the v1 diagnostic's own marker
    # name, bumped so this re-runs once more on the next boot even
    # though a v1 marker is already on disk from the earlier deploy -
    # v1 logged the error WRAPPER, not the error itself (blank "error:
    # " - see _serialize_batch_result_error()'s own docstring), so its
    # run didn't actually get the real cause into the logs. A distinct
    # marker filename, not deleting/rewriting the v1 one, keeps this
    # the same "one-off, never re-run once its own marker exists"
    # pattern for THIS (v2) diagnostic specifically.
    base = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH") or os.path.dirname(__file__)
    return os.path.join(base, ".top100_batch_01xa_diagnostic_v2_done")


def diagnose_batch_01xa_once(log=print):
    """One-off, marker-guarded boot diagnostic (24 Sep 2026, owner-
    reported): tonight's first real Top 100 batch
    (msgbatch_01XA46gE4evNBqLnZdy2EACR) came back 100% errored, and
    poll_and_ingest_batch()'s own per-ticker log line never carried the
    actual error - only "errored, skipped" - so the root cause was
    invisible in production. Anthropic stores batch results for 29 days
    (see the Batches API's own "Key Facts"), so this retrieves that
    SPECIFIC batch directly, logs the first 3 errored results' errors
    (the FULL serialized error, truncated - never the request content),
    then writes the v2 marker so this never runs again. Same marker-
    guarded, read-the-marker-first, "never allowed to stop the site
    serving" pattern as nightly_scan.py's own one-off cleanup functions
    (see server.py's lifespan() for where this is wired in) - this gets
    the real cause into Railway logs on the very next deploy, instead of
    waiting a full night for a fresh batch to error the same way.

    v2 (same day): v1 of this diagnostic logged result.result.error's
    own .type/.message directly and printed a BLANK error ("error: ")
    for every result - result.result.error is an ErrorResponse wrapper
    (its own .type is always the literal "error", no top-level
    .message at all); the real detail lives on its INNER .error object.
    See _serialize_batch_result_error()'s own docstring. This version
    logs the whole serialized error object instead of picking
    attributes, and runs under a NEW marker file so it fires again on
    the next boot even though v1 already wrote its own marker."""
    marker = _batch_01xa_diagnostic_marker_path()
    if os.path.exists(marker):
        return
    try:
        import anthropic
        client = anthropic.Anthropic()
        shown = 0
        for result in client.messages.batches.results(_BATCH_01XA_DIAGNOSTIC_TARGET):
            if result.result.type != "errored":
                continue
            err = getattr(result.result, "error", None)
            detail = _serialize_batch_result_error(err)
            log(f"[top100] one-off diagnostic {_BATCH_01XA_DIAGNOSTIC_TARGET} "
                f"error #{shown + 1}: {detail}")
            shown += 1
            if shown >= 3:
                break
        if shown == 0:
            log(f"[top100] one-off diagnostic {_BATCH_01XA_DIAGNOSTIC_TARGET}: "
                f"no errored results found (results may have expired past Anthropic's "
                f"29-day retention, or this batch didn't actually error)")
    except Exception as e:
        log(f"[top100] one-off diagnostic {_BATCH_01XA_DIAGNOSTIC_TARGET} failed: {e}")

    try:
        with open(marker, "w") as f:
            f.write(datetime.now(timezone.utc).isoformat())
    except OSError as e:
        log(f"[top100] one-off diagnostic: could not write marker file: {e}")


_BATCH_INSPECTOR_EXCERPT_CHARS = 600


def inspect_batch_results(results, expected_map=None, schema_mode_map=None):
    """Pure parsing logic for the Admin Dashboard's Batch inspector
    panel - no network, no Anthropic client. `results` is a list of
    plain dicts, one per Batches API result, already pulled off the
    real SDK object by run_batch_inspector() below: {"custom_id",
    "type" ("succeeded"|"errored"|"canceled"|"expired"), "stop_reason"
    (succeeded only), "input_tokens"/"output_tokens" (succeeded only),
    "text" (succeeded only - the raw response text), "error_detail"
    (non-succeeded only - _serialize_batch_result_error()'s own
    output)}. `expected_map` is {custom_id: [ticker, ...], ...} from
    top100_store.expected_tickers_for_batch() - None/missing entries
    mean "not available for this batch" (see that function's own
    docstring).

    Returns (rows, summary). Each row: {"custom_id", "type",
    "stop_reason", "input_tokens", "output_tokens", "expected_tickers"
    (list or None - entrant tickers IN ORDER, the request's own input
    order), "tickers_echoed" (list, the ticker strings the response
    ACTUALLY used as companies[] keys - a ticker echoed as "RG1"
    instead of the expected "RG1.AX" shows up here under its own,
    different string), "items_count", "excerpt" (first
    _BATCH_INSPECTOR_EXCERPT_CHARS chars of the raw text/error, or None
    when the request fully matched its expected tickers), "full_text"
    (the COMPLETE raw text/error detail, untruncated, for EVERY
    request regardless of match status - stored score viewer's own
    ticker-search box, 3 Oct 2026, owner-directed, reads this so the
    owner can see a fully-matched request's own raw response too, not
    only a non-matching one's excerpt), "blank" (bool - A3, 5 Oct 2026,
    "make the blank replies readable": True exactly when top100_
    engine._whole_response_is_degenerate() would classify this
    request's response as the whole-blank shape - i.e. the SAME test
    poll_and_ingest_batch() itself uses to record "request_blank",
    reproduced here read-only against the already-fetched text/
    expected tickers, never re-parsing anything differently; always
    False for a non-succeeded result or when expected tickers aren't
    known for this batch), "schema_mode" (COMMIT 4 of instruction_
    top200_unrated_and_blank_replies_combined.md, 5 Oct 2026 - "legacy"/
    "ticker_first" from `schema_mode_map`, or None when that map itself
    is None - i.e. not available for this batch, same convention as
    "expected_tickers"), "packed_or_solo" ("packed"/"solo"/None - derived
    from `expected`'s own entrant count when known, else from items_
    count; None when neither tells us anything, e.g. a fully errored
    request with no expected map)}.

    summary = {"total", "fully_matched", "partially_matched", "empty",
    "errored"} - mutually exclusive, sums to total. A request is
    "errored" whenever result.type != "succeeded" (covers errored/
    canceled/expired alike); "empty" when it succeeded but its own
    companies[] array parsed to zero items; "partially_matched" when
    expected tickers are known and at least one of them is missing from
    tickers_echoed BY STRING (catches both a genuinely dropped item and
    a mismatched-ticker-string echo); "fully_matched" otherwise (every
    expected ticker's own exact string was found, or expected tickers
    simply aren't known for this batch and at least one item parsed)."""
    expected_map = expected_map or {}
    rows = []
    fully_matched = partially_matched = empty = errored = 0
    for r in results:
        custom_id = r.get("custom_id")
        rtype = r.get("type")
        expected = expected_map.get(custom_id)
        row = {
            "custom_id": custom_id,
            "type": rtype,
            "stop_reason": r.get("stop_reason"),
            "input_tokens": r.get("input_tokens"),
            "output_tokens": r.get("output_tokens"),
            "expected_tickers": list(expected) if expected is not None else None,
            "blank": False,
            "schema_mode": (schema_mode_map.get(custom_id, "legacy") if schema_mode_map is not None else None),
        }
        if rtype != "succeeded":
            row["tickers_echoed"] = []
            row["items_count"] = 0
            detail = r.get("error_detail") or ""
            row["excerpt"] = detail[:_BATCH_INSPECTOR_EXCERPT_CHARS] or None
            row["full_text"] = detail
            errored += 1
            row["packed_or_solo"] = (
                ("solo" if len(expected) == 1 else "packed") if expected else None
            )
            rows.append(row)
            continue
        text = r.get("text") or ""
        row["full_text"] = text
        try:
            parsed = _parse_response_json(text, expected_tickers=expected)
        except Exception:
            parsed = {}
        echoed = sorted(parsed.keys())
        row["tickers_echoed"] = echoed
        row["items_count"] = len(echoed)
        if expected:
            row["packed_or_solo"] = "solo" if len(expected) == 1 else "packed"
        elif echoed:
            row["packed_or_solo"] = "solo" if len(echoed) == 1 else "packed"
        else:
            row["packed_or_solo"] = None
        if expected is not None:
            try:
                _companies = json.loads(text).get("companies")
                if isinstance(_companies, list):
                    row["blank"] = _whole_response_is_degenerate(_companies, expected)
            except Exception:
                pass
        # Classification is always by ticker STRING match when the
        # expected map exists (expected_tickers_for_batch() backfills it
        # for every batch submitted from now on - see that function's
        # own docstring) - row["match_basis"] names explicitly which
        # basis this particular row used, so a batch ingested before
        # that column existed never silently looks "fully matched" on
        # nothing more than "at least one item parsed".
        row["match_basis"] = (
            "ticker string match" if expected is not None
            else "count match only - expected map unavailable"
        )
        missing = (set(expected) - set(echoed)) if expected is not None else set()
        if not echoed:
            row["excerpt"] = text[:_BATCH_INSPECTOR_EXCERPT_CHARS] or None
            empty += 1
        elif missing:
            row["excerpt"] = text[:_BATCH_INSPECTOR_EXCERPT_CHARS] or None
            partially_matched += 1
        else:
            row["excerpt"] = None
            fully_matched += 1
        rows.append(row)
    summary = {
        "total": len(results), "fully_matched": fully_matched,
        "partially_matched": partially_matched, "empty": empty, "errored": errored,
    }
    return rows, summary


def run_batch_inspector(batch_id, log=print):
    """Live wrapper - fetches `batch_id`'s results from the Anthropic
    Batches API (retrievable for 29 days after the batch ended, same
    retention diagnose_batch_01xa_once() above already relies on; this
    works for an ALREADY-INGESTED batch exactly like that one-off does,
    since poll_and_ingest_batch() only ever deletes its OWN top100_
    batch_state row, never anything on Anthropic's side) and runs
    inspect_batch_results() against the real data. expected_map comes
    from top100_store.expected_tickers_for_batch(batch_id) - None/
    missing for a batch ingested before that column existed.
    schema_mode_map (COMMIT 4 of instruction_top200_unrated_and_blank_
    replies_combined.md, 5 Oct 2026) comes from top100_store.schema_
    mode_for_batch(batch_id) the same way.

    Returns (rows, summary), or (None, None) if the fetch itself
    failed (network/SDK/bad batch id - the caller shows the exception
    message). Logs exactly one "[batch_inspector]" summary line on
    success, so the owner can read the same figures from the Railway
    log without opening the page."""
    try:
        import anthropic
        client = anthropic.Anthropic()
        raw_results = []
        for result in client.messages.batches.results(batch_id):
            rtype = result.result.type
            if rtype == "succeeded":
                msg = result.result.message
                text = next((b.text for b in msg.content if b.type == "text"), "")
                raw_results.append({
                    "custom_id": result.custom_id, "type": rtype,
                    "stop_reason": getattr(msg, "stop_reason", None),
                    "input_tokens": getattr(msg.usage, "input_tokens", None),
                    "output_tokens": getattr(msg.usage, "output_tokens", None),
                    "text": text,
                })
            else:
                err = getattr(result.result, "error", None)
                raw_results.append({
                    "custom_id": result.custom_id, "type": rtype,
                    "error_detail": _serialize_batch_result_error(err),
                })
    except Exception as e:
        log(f"[batch_inspector] batch {batch_id}: fetch failed - {e}")
        return None, None

    expected_map = top100_store.expected_tickers_for_batch(batch_id)
    schema_mode_map = top100_store.schema_mode_for_batch(batch_id)
    rows, summary = inspect_batch_results(raw_results, expected_map, schema_mode_map)
    log(f"[batch_inspector] batch {batch_id}: {summary['total']} requests - "
        f"fully matched {summary['fully_matched']}, partially matched {summary['partially_matched']}, "
        f"empty {summary['empty']}, errored {summary['errored']}"
        + (" (expected tickers not available for this batch)" if expected_map is None else ""))
    return rows, summary


def batch_inspector_csv(rows):
    """A3 (5 Oct 2026, owner-directed, diagnostics only - "make the
    blank replies readable for Andrew and the Director"): `rows` (run_
    batch_inspector()'s own output) rendered as CSV text, one row per
    request - custom id, entrant tickers IN ORDER (expected_tickers,
    semicolon-joined so a single CSV cell survives the comma-delimited
    format), entrant count, items returned, tickers echoed (same join
    convention), stop reason, input tokens, output tokens, blank yes/
    no, schema_mode, packed_or_solo (COMMIT 4 of instruction_top200_
    unrated_and_blank_replies_combined.md, 5 Oct 2026 - blank string
    when not available for this batch/row). Pure formatting - no
    network, no parsing beyond what rows already carries. Returns the
    CSV text (str); the caller (the Admin Batch inspector panel) hands
    it to st.download_button."""
    import csv
    import io
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "custom_id", "entrant_tickers", "entrant_count", "items_returned",
        "tickers_echoed", "stop_reason", "input_tokens", "output_tokens", "blank",
        "schema_mode", "packed_or_solo",
    ])
    for row in rows:
        expected = row.get("expected_tickers")
        writer.writerow([
            row.get("custom_id"),
            ";".join(expected) if expected is not None else "",
            len(expected) if expected is not None else "",
            row.get("items_count"),
            ";".join(row.get("tickers_echoed") or []),
            row.get("stop_reason") or "",
            row.get("input_tokens") if row.get("input_tokens") is not None else "",
            row.get("output_tokens") if row.get("output_tokens") is not None else "",
            "yes" if row.get("blank") else "no",
            row.get("schema_mode") or "",
            row.get("packed_or_solo") or "",
        ])
    return buf.getvalue()


def _pool_expansion_v200_marker_path():
    """Pool expansion one-off persistence seed (1 Oct 2026, owner
    decision, Commit 5 of instruction_dcf_unreliable_pool_step4_
    nightly.md) - marker file guarding select_top100_pool()'s own one-
    time expansion-persistence seeding (see that function's own comment
    at the seeding call site) to fire exactly once, the first nightly
    run under the POOL_SIZE 100->200 bump, never again afterward. Same
    "one file per one-off" convention as _pool_presence_v6_seed_marker_
    path() above."""
    base = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH") or os.path.dirname(__file__)
    return os.path.join(base, ".top100_pool_expansion_v200_seed_done")


def _pool_presence_v6_seed_marker_path():
    base = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH") or os.path.dirname(__file__)
    return os.path.join(base, ".top100_pool_presence_v6_seed_done")


def seed_pool_presence_for_v6_once(log=print):
    """One-off, marker-guarded boot seeding (1 Oct 2026, owner decision,
    newcomer persistence filter): the v5->v6 RUBRIC_VERSION bump is the
    first night _true_newcomer_gated() is active at all - without this,
    every ticker ALREADY in the pool before this shipped would read as
    a brand-new, zero-night newcomer (no pool_presence row yet) and get
    wrongly deferred, even though many have been in the pool and scored
    for months. Seeds top100_store.pool_presence for every ticker in
    the latest pool snapshot (current_pool() + current_asx_extension())
    with consecutive_nights=3 (exempt from the gate immediately) if the
    ticker already carries a score under ANY PREVIOUS rubric (top100_
    store.latest_score_previous_rubric() - a v4/v5 score proves it has
    been in the pool and scored before, so the v6 full re-score itself
    must never be held back by this unrelated filter), else consecutive_
    nights=1 (a genuinely never-scored newcomer starts its real 3-night
    count from today, exactly as if it had just entered the pool for
    the first time). Uses top100_store.seed_pool_presence()'s own
    INSERT OR IGNORE, so this is safe even if update_pool_presence()
    (the ordinary nightly writer) somehow already ran first for a given
    ticker. Same marker-guarded, "never allowed to stop the site
    serving" pattern as diagnose_batch_01xa_once() above - see that
    function's own docstring; wired into server.py's lifespan() the
    same way."""
    marker = _pool_presence_v6_seed_marker_path()
    if os.path.exists(marker):
        return
    try:
        pool = top100_store.current_pool() + top100_store.current_asx_extension()
        today = datetime.now(timezone.utc).date().isoformat()
        presence = {}
        seeded_3 = seeded_1 = 0
        for row in pool:
            ticker = row["ticker"]
            if top100_store.latest_score_previous_rubric(ticker, MODEL_TOP100, RUBRIC_VERSION) is not None:
                presence[ticker] = 3
                seeded_3 += 1
            else:
                presence[ticker] = 1
                seeded_1 += 1
        top100_store.seed_pool_presence(presence, today)
        log(f"[top100] pool presence seeded for RUBRIC_VERSION {RUBRIC_VERSION}: "
            f"{seeded_3} ticker(s) set to 3 (prior-rubric history), "
            f"{seeded_1} ticker(s) set to 1 (true newcomers)")
    except Exception as e:
        log(f"[top100] pool presence seeding failed: {e}")

    try:
        with open(marker, "w") as f:
            f.write(datetime.now(timezone.utc).isoformat())
    except OSError as e:
        log(f"[top100] pool presence seeding: could not write marker file: {e}")


def run_nightly(log=print):
    """The scheduler's own entry point (scheduler_engine._run_top100 -
    same "let it raise, retry-cap sees a real failure" contract as
    quote_recorder's own run_*_recorder()): re-selects the pool, then
    polls any in-flight batch before submitting a new one - see this
    module's own docstring for why poll-then-submit, never both at
    once."""
    select_top100_pool(log=log)
    poll_and_ingest_batch(log=log)
    submit_nightly_batch(log=log)


def run_single_test_call(ticker, company_name=None):
    """ONE real, non-batched API call for a single company - the
    task's own "make ONE real single-company API test call" ask.
    Never touches the batch-state row or the pool (a standalone
    diagnostic, not part of the nightly flow) - does persist the score
    via top100_store.save_score(), same as a real batch result would,
    so the test call's own result is immediately visible on the page
    rather than thrown away.

    Results-driven Top 100 refresh (27 Sep 2026): the score key is now
    the natural RP/D derivation (_natural_score_key(), same as the
    force=True path - see submit_nightly_batch()'s own docstring for why
    that's the right choice for an explicit, standalone call), looking
    up this ticker's own most_recent_quarter from the current pool
    (current_pool() + current_asx_extension()) if it's in there, else
    None for a ticker outside the pool entirely (this call's whole
    point is to test-score an arbitrary ticker, pooled or not). Returns
    {"ticker","dims","not_rated","inversion_scenario","inversion_
    severity","current_headwind","market_structure","market_structure_
    comment","one_foot_hurdle","one_foot_comment","munger_quality",
    "munger_comment","big_wave","big_wave_comment","input_tokens",
    "output_tokens","cost_usd"} - standard, non-batch pricing (this
    call does not go through the Batches API), reported honestly as
    such.

    v6 packed requests (1 Oct 2026, owner-directed): internally routed
    through the same list-based _request_params()/_parse_response_
    json() path submit_nightly_batch() uses, with a one-entrant list -
    this function's own external contract (arguments, return shape) is
    completely unchanged; only the internal plumbing is shared now."""
    import anthropic
    client = anthropic.Anthropic()
    pool_row = next(
        (r for r in top100_store.current_pool() + top100_store.current_asx_extension()
         if r["ticker"] == ticker),
        None,
    )
    entrants = [{
        "ticker": ticker,
        "company_name": company_name or (pool_row.get("company_name") if pool_row else None),
        "sector": pool_row.get("sector") if pool_row else None,
    }]
    params = _request_params(entrants)
    resp = client.messages.create(**params)
    text = next((b.text for b in resp.content if b.type == "text"), "")
    _matched_by = {}
    parsed_by_ticker = _parse_response_json(text, expected_tickers=[ticker], matched_by_out=_matched_by)
    if ticker not in parsed_by_ticker:
        raise ValueError(f"{ticker} missing from the model's own response")
    (dims, not_rated, inversion_scenario, inversion_severity, current_headwind,
     market_structure, market_structure_comment,
     one_foot_hurdle, one_foot_comment,
     munger_quality, munger_comment, big_wave, big_wave_comment) = parsed_by_ticker[ticker]
    input_tokens = getattr(resp.usage, "input_tokens", 0) or 0
    output_tokens = getattr(resp.usage, "output_tokens", 0) or 0
    cost = (input_tokens / 1_000_000) * TOP100_INPUT_USD_PER_MTOK + \
        (output_tokens / 1_000_000) * TOP100_OUTPUT_USD_PER_MTOK
    most_recent_quarter = pool_row.get("most_recent_quarter") if pool_row else None
    score_key = _natural_score_key({"most_recent_quarter": most_recent_quarter})
    top100_store.save_score(
        ticker=ticker, quarter=score_key, model=MODEL_TOP100, rubric_version=RUBRIC_VERSION,
        dims=dims, not_rated=not_rated,
        most_recent_quarter=most_recent_quarter,
        inversion_scenario=inversion_scenario, inversion_severity=inversion_severity,
        current_headwind=current_headwind,
        market_structure=market_structure, market_structure_comment=market_structure_comment,
        one_foot_hurdle=one_foot_hurdle, one_foot_comment=one_foot_comment,
        munger_quality=munger_quality, munger_comment=munger_comment,
        big_wave=big_wave, big_wave_comment=big_wave_comment,
        matched_by=_matched_by.get(ticker),
        prompt=json.dumps(params), raw_response=text,
    )
    return {
        "ticker": ticker, "dims": dims, "not_rated": not_rated,
        "inversion_scenario": inversion_scenario, "inversion_severity": inversion_severity,
        "current_headwind": current_headwind,
        "market_structure": market_structure, "market_structure_comment": market_structure_comment,
        "one_foot_hurdle": one_foot_hurdle, "one_foot_comment": one_foot_comment,
        "munger_quality": munger_quality, "munger_comment": munger_comment,
        "big_wave": big_wave, "big_wave_comment": big_wave_comment,
        "input_tokens": input_tokens, "output_tokens": output_tokens, "cost_usd": cost,
    }


def estimate_batch_cost_usd(input_tokens, output_tokens,
                             cache_creation_input_tokens=0, cache_read_input_tokens=0):
    """Batch API pricing (BATCH_DISCOUNT, 50% of standard) applied to
    real token counts - the nightly log's own cost-telemetry line, and
    poll_and_ingest_batch()'s own summary.

    Honest cost accounting (1 Oct 2026, owner-directed): `input_tokens`
    is priced at the plain standard rate; `cache_creation_input_tokens`
    (a cache MISS - the content had to be written to the cache) at
    CACHE_WRITE_MULTIPLIER x that rate; `cache_read_input_tokens` (a
    cache HIT) at CACHE_READ_MULTIPLIER x - all three, plus output
    tokens, THEN get the batch discount, same as before. The two new
    params default to 0 so every existing caller (admin_data_audit's
    own worst-case estimate in app.py, run_single_test_call's non-batch
    cost line) that only ever passes plain input/output tokens is
    unaffected."""
    standard = (
        (input_tokens / 1_000_000) * TOP100_INPUT_USD_PER_MTOK
        + (cache_creation_input_tokens / 1_000_000) * TOP100_INPUT_USD_PER_MTOK * CACHE_WRITE_MULTIPLIER
        + (cache_read_input_tokens / 1_000_000) * TOP100_INPUT_USD_PER_MTOK * CACHE_READ_MULTIPLIER
        + (output_tokens / 1_000_000) * TOP100_OUTPUT_USD_PER_MTOK
    )
    return standard * BATCH_DISCOUNT


def degenerate_accepted_rows(model=MODEL_TOP100):
    """A2 (5 Oct 2026, owner-directed, "owner button to re-send the
    companies already locked out"): every stored score row with
    degenerate_accepted=True under the CURRENT rubric for `model` -
    {"ticker", "company_name", "quarter", "scored_at"}, one dict per
    row, sorted by ticker. Built on top100_store.latest_scores_for_
    model() (already read-only, one row per ticker) rather than a new
    query - a genuine NOT RATED (three or more zero dimensions with
    real justifications, degenerate_accepted False) is never included,
    by construction: this filters on exactly the column _save_
    degenerate_as_not_rated() sets, nothing else. `company_name` comes
    from the current pool/ASX extension when the ticker is still
    pooled, else falls back to the ticker itself (a company that has
    since dropped out of the pool but still carries an old degenerate-
    accepted row shouldn't disappear from this list silently)."""
    latest = top100_store.latest_scores_for_model(model, RUBRIC_VERSION)
    pool_by_ticker = {
        row["ticker"]: row
        for row in (top100_store.current_pool() + top100_store.current_asx_extension())
    }
    out = []
    for ticker, row in latest.items():
        if not row.get("degenerate_accepted"):
            continue
        out.append({
            "ticker": ticker,
            "company_name": (pool_by_ticker.get(ticker) or {}).get("company_name") or ticker,
            "quarter": row.get("quarter"),
            "scored_at": row.get("scored_at"),
        })
    out.sort(key=lambda r: r["ticker"])
    return out


def estimate_degenerate_resend_cost_usd(rows):
    """Rough pre-action cost estimate for A2's "Clear and re-send"
    button - same packed-request estimate submit_nightly_batch() itself
    logs before submitting (_estimate_request_tokens()/_ESTIMATED_
    OUTPUT_TOKENS_PER_ENTRANT, packed up to TOP100_COMPANIES_PER_
    REQUEST at a time), applied to `rows` (degenerate_accepted_rows()'
    own output) as if they were tonight's only entrants. An order-of-
    magnitude figure for the owner to see BEFORE clicking the button,
    not a billing-accurate one - same caveat as every other pre-
    submission estimate in this module."""
    if not rows:
        return 0.0
    entrants = [{"ticker": r["ticker"], "company_name": r["company_name"], "sector": None} for r in rows]
    packs = [entrants[i:i + TOP100_COMPANIES_PER_REQUEST]
             for i in range(0, len(entrants), TOP100_COMPANIES_PER_REQUEST)]
    est_input_tokens = sum(_estimate_request_tokens(pack) for pack in packs)
    est_output_tokens = len(entrants) * _ESTIMATED_OUTPUT_TOKENS_PER_ENTRANT
    return estimate_batch_cost_usd(est_input_tokens, est_output_tokens)


def clear_degenerate_accepted_rows(rows, model=MODEL_TOP100, log=print):
    """A2's "Clear and re-send" action: deletes each of `rows`
    (degenerate_accepted_rows()' own output - never re-queried here,
    so the button deletes EXACTLY what the panel displayed, not
    whatever matches the filter at click time) via top100_store.
    delete_score() - the same path the 3 Oct retroactive sweep
    (run_degenerate_sweep_once()) used - so the normal nightly
    _unscored_tickers() picks them up and re-sends them like any other
    never-scored entrant. Triggers no batch itself; nothing happens
    until the next scheduled or owner-initiated submission. Logs the
    exact owner-specified line. Returns the list of tickers cleared."""
    cleared = []
    for row in rows:
        top100_store.delete_score(row["ticker"], row["quarter"], model, RUBRIC_VERSION)
        cleared.append(row["ticker"])
    if cleared:
        log(f"[top100] owner cleared {len(cleared)} degenerate-accepted row(s): "
            f"{', '.join(sorted(cleared))}")
    return cleared


def exhausted_failure_rows(model=MODEL_TOP100):
    """COMMIT 2 of instruction_top200_unrated_and_blank_replies_combined.md
    (5 Oct 2026, Director-directed, "A2's panel extended to list+clear
    exhausted-attempt companies too") - every stored FAILURE under the
    current rubric whose attempts have reached its own exhaustion
    ceiling (_failure_exhausted() - 5 for request_blank, 3 for every
    other reason), i.e. every UNRATED_FAILED ticker that is NOT also
    degenerate_accepted (that case already has its own list via
    degenerate_accepted_rows() - a ticker can only ever be in one of
    the two: a failure row and a score row are mutually exclusive per
    (ticker, model, rubric_version)). {"ticker", "company_name",
    "reason", "attempts", "failed_at"}, sorted by ticker. A NOT RATED-
    by-model row (score_row.not_rated True, degenerate_accepted False)
    has no failure row at all and can never appear here - by
    construction, since this reads top100_store.score_failures_for_
    model() alone, never top100_scores."""
    failures = top100_store.score_failures_for_model(model, RUBRIC_VERSION)
    pool_by_ticker = {
        row["ticker"]: row
        for row in (top100_store.current_pool() + top100_store.current_asx_extension())
    }
    out = []
    for ticker, failure in failures.items():
        if not _failure_exhausted(failure):
            continue
        out.append({
            "ticker": ticker,
            "company_name": (pool_by_ticker.get(ticker) or {}).get("company_name") or ticker,
            "reason": failure.get("reason"),
            "attempts": failure.get("attempts"),
            "failed_at": failure.get("failed_at"),
        })
    out.sort(key=lambda r: r["ticker"])
    return out


def clear_exhausted_failure_rows(rows, model=MODEL_TOP100, log=print):
    """COMMIT 2's own "Clear and re-send" action for exhausted_failure_
    rows()' output - deletes each failure row via top100_store.clear_
    score_failure() (exactly what `rows` was given, never re-queried
    here, same as clear_degenerate_accepted_rows()' own contract) so
    the normal nightly _unscored_tickers() treats this ticker as a
    brand-new entrant again, attempts reset to 0. Triggers no batch
    itself. Logs the exact owner-specified line. Returns the list of
    tickers cleared."""
    cleared = []
    for row in rows:
        top100_store.clear_score_failure(row["ticker"], model, RUBRIC_VERSION)
        cleared.append(row["ticker"])
    if cleared:
        log(f"[top100] owner cleared {len(cleared)} exhausted-failure row(s): "
            f"{', '.join(sorted(cleared))}")
    return cleared


# -----------------------------------------------------------------
# COMMIT 1 of instruction_top200_unrated_and_blank_replies_combined.md
# (5 Oct 2026, Director-directed): automatic per-market coverage count
# - no change to selection, to the public page, or to any request.
# -----------------------------------------------------------------

# Task's own threshold: a market needs at least this many RESOLVED
# (rated + unrated_model + unrated_failed - waiting companies excluded,
# since they haven't resolved to anything yet) companies before the
# unrated-share warning is meaningful at all, and fires only once the
# unrated share of those resolved companies exceeds this percent.
UNRATED_SHARE_WARNING_MIN_RESOLVED = 10
UNRATED_SHARE_WARNING_PCT = 10

# "Addition" (same commit): a large, well-known company ending up
# UNRATED_FAILED is itself a signal something's wrong - these are the
# seven index universes the task names, read from whatever each has
# saved (no network call - scan_store.load_scan_raw() is a disk read,
# and a universe with nothing saved/stale contributes no tickers,
# never raises).
LARGE_COMPANY_UNIVERSES = (
    "S&P 500", "Nasdaq 100", "Dow Jones 30",
    "ASX 200", "FTSE 100", "TSX 60", "Nikkei 225",
)


def _large_company_tickers():
    """Ticker set across every LARGE_COMPANY_UNIVERSES' own last saved
    scan (scan_store.load_scan_raw(), same default allow_private=False
    every other caller in this module uses - none of these seven index
    universes is ever privacy-gated in practice, but this keeps the
    convention uniform). Used only to flag an UNRATED_FAILED company
    worth a second look in Admin - never read by selection/scoring/
    ranking."""
    tickers = set()
    for universe in LARGE_COMPANY_UNIVERSES:
        try:
            payload = scan_store.load_scan_raw(universe)
        except Exception:
            payload = None
        if not payload:
            continue
        for row in payload.get("rows") or []:
            t = (row.get("Ticker") or "").strip().upper()
            if t:
                tickers.add(t)
    return tickers


def company_status(score_row, failure):
    """One of "rated"/"unrated_model"/"unrated_failed"/"waiting" - the
    task's own STEP A0 terms, worked out from stored data alone (no
    network call):
      - RATED: score_row exists, not_rated is falsy.
      - UNRATED_MODEL: score_row exists, not_rated is true,
        degenerate_accepted is falsy (the model's own judgement).
      - UNRATED_FAILED: EITHER score_row exists with not_rated AND
        degenerate_accepted true (the 2-consecutive-degenerate accept
        path), OR there is no score_row at all but `failure` has
        reached _failure_exhausted() under the current rubric (a
        request_blank/parse_error/missing_from_response/errored
        ticker that will never be retried again for this rubric).
      - WAITING: everything else - no score_row, and either no
        failure recorded at all, or one that hasn't exhausted its
        retries yet (still eligible for a future attempt)."""
    if score_row is not None:
        if score_row.get("not_rated"):
            return "unrated_failed" if score_row.get("degenerate_accepted") else "unrated_model"
        return "rated"
    if failure is not None and _failure_exhausted(failure):
        return "unrated_failed"
    return "waiting"


# -----------------------------------------------------------------
# COMMIT 3 of instruction_top200_unrated_and_blank_replies_combined.md
# (5 Oct 2026, Director-directed, owner-approved for build+push-on-go,
# switch OFF by default): unrated companies never hold one of the 200
# places - the next candidate in line on merit takes it instead.
# -----------------------------------------------------------------

# Never walk past this many positions beyond POOL_SIZE in the merit
# order looking for 200 RATED/WAITING candidates - the task's own cap,
# so a market flooded with unrated candidates can't make selection
# walk the entire merit-ordered list.
TOP200_BACKFILL_MAX = 60


def is_backfill_live():
    """The switch - same unset/""/0/off=OFF, 1/on=ON pattern as
    financials_classifier.is_financials_store_live()/scan_store.
    is_private_universe(). Re-read on every call (cheap - one env var
    lookup), never cached, so a changed Railway variable takes effect
    on the next request without a redeploy. Claude Code never sets
    this - the Director does, on Railway, after Andrew has read the
    dry run (COMMIT 3's own owner preview) and said yes."""
    raw = (os.environ.get("TOP200_BACKFILL_LIVE") or "").strip().lower()
    return raw in ("1", "on")


def preview_universes():
    """PART 5 STEP 5.2 of instruction_health_fixes_chart_and_new_
    markets.md (8 Oct 2026, Director-directed): TOP200_PREVIEW_
    UNIVERSES - comma-separated universe names (e.g. "FTSE 100, DAX"),
    re-split and re-read on every call, same unset/empty="nothing
    reads this list" pattern as every other switch in this module.
    Unset/empty -> [] - the owner preview panel (app.py) and this
    module's own cost-estimate log line are the ONLY two readers;
    _eligible_scan_payloads()/select_top100_pool()/save_pool() never
    read this at all, so an empty list changes nothing about which
    universes are eligible for the real, public pool."""
    raw = os.environ.get("TOP200_PREVIEW_UNIVERSES") or ""
    return [u.strip() for u in raw.split(",") if u.strip()]


def _log_preview_universe_cost_estimate(log=print):
    """PART 5 STEP 5.2 (8 Oct 2026, Director-directed): one
    informational log line per PRIVATE universe with a saved scan -
    reusing market_readiness_engine.top200_dry_run()'s own {candidate_
    count, no_stored_score_count, estimated_cost_usd} (the exact
    number the owner-only Market readiness panel already shows per
    universe - see app._render_market_readiness_panel()'s own "Top 200
    effect, dry run" section) so Andrew can see the one-time cost of
    adding any private market to TOP200_PREVIEW_UNIVERSES before ever
    setting it. Fires every run regardless of TOP200_PREVIEW_
    UNIVERSES's own value - purely advisory, nothing selected, nothing
    scored, no network call (scan_store.load_scan_raw() is a disk
    read), never raises - a reporting side effect, same posture as
    _log_coverage()."""
    try:
        for universe in scan_store.list_saved_universes(include_private=True):
            if not scan_store.is_private_universe(universe):
                continue
            payload = scan_store.load_scan_raw(universe, allow_private=True)
            rows = (payload or {}).get("rows") or []
            if not rows:
                continue
            dry = market_readiness_engine.top200_dry_run(rows)
            log(f"[top200-preview] {universe!r}: {dry['candidate_count']} candidate(s), "
                f"{dry['no_stored_score_count']} with no stored score yet, "
                f"est. one-time cost ${dry['estimated_cost_usd']:.2f}")
    except Exception as e:
        log(f"[top200-preview] cost-estimate logging failed (non-fatal): {e}")


def preview_candidates_by_country():
    """PART 5 STEP 5.2: {country: [row, ...]} across every universe
    named in preview_universes(), each universe's rows read DIRECTLY
    via scan_store.load_scan_raw(universe, allow_private=True) - the
    exact same direct read market_readiness_engine.universe_
    readiness() already uses (PART 3) - never through _eligible_scan_
    payloads()/_build_best_by_ticker()/select_top100_pool(). This
    function is never called by, and never calls, any of those three
    - it cannot affect, and is never affected by, the real public
    pool's own selection.

    Row shape matches market_readiness_engine.top20_by_value_score()'s
    own per-row dict (ticker/name/currency/price/intrinsic_value/
    mos_pct/growth_used/data_path/dividend_yield_pct/value_score),
    plus "universe" (which saved file this candidate came from).
    "market" grouping is peer_context.market_for(ticker) - inherits
    that function's own known gap for the five European suffixes
    (flagged, not fixed, in the PART 4 report): a DAX/CAC 40/AEX/SMI/
    OMX Stockholm 30 ticker is grouped under "USA" here too until that
    gap is fixed; this function does not work around it.

    Sorted within each country by value_score descending, rows with
    no stored score at all last (never dropped - same "never show
    nothing" rule top20_by_value_score() itself already follows).
    Returns {} if preview_universes() is empty - the one and only
    condition under which this returns nothing, matching every other
    switch's "unset = nothing" contract in this module."""
    by_country = {}
    for universe in preview_universes():
        payload = scan_store.load_scan_raw(universe, allow_private=True)
        rows = (payload or {}).get("rows") or []
        for r in rows:
            ticker = r.get("Ticker")
            if not ticker:
                continue
            market = peer_context.market_for(ticker)
            by_country.setdefault(market, []).append({
                "ticker": ticker, "name": r.get("Company Name"),
                "currency": fcf_valuation_engine.trading_currency_for(ticker, info=None),
                "price": r.get("Price"), "intrinsic_value": r.get("Intrinsic Value"),
                "mos_pct": r.get("MOS %"), "growth_used": r.get("Growth Used"),
                "data_path": r.get("FCF Source"), "dividend_yield_pct": r.get("Dividend Yield %"),
                "value_score": r.get("Long Score"), "universe": universe,
            })
    for market, rows in by_country.items():
        rows.sort(key=lambda r: (r["value_score"] is None, -(r["value_score"] or 0)))
    return by_country


def _apply_backfill(merit_ordered, model=MODEL_TOP100):
    """Pure function, no store write, no log call - the one computation
    shared by the switch-ON selection path, the switch-OFF dry-run log
    line, and the owner preview panel, so all three can never disagree
    with each other. `merit_ordered`: the FULL value_score-descending
    candidate list exactly as select_top100_pool() builds it today
    (sorted(best_by_ticker.values(), key=value_score, reverse=True)),
    not yet sliced to POOL_SIZE.

    Walks `merit_ordered` in order, classifying each candidate via
    company_status(). A candidate that is "unrated_model" or
    "unrated_failed" is set aside and does not count; RATED or WAITING
    counts. Stops counting at POOL_SIZE, and never looks past position
    POOL_SIZE + TOP200_BACKFILL_MAX regardless.

    A3 amendment of instruction_top200_amendments_and_currency_view.md
    (5 Oct 2026, Director-directed, "the public list is filled to 200
    with rated companies"): once the scored set above is built, if W of
    its own "counted" rows are WAITING, the SAME walk continues (same
    merit order, same POOL_SIZE + TOP200_BACKFILL_MAX ceiling, picking
    up exactly where the scored-set walk left off) for up to W further
    candidates that are ALREADY RATED - every UNRATED_MODEL/UNRATED_
    FAILED/WAITING candidate encountered along the way is skipped
    (never added to `set_aside` a second time, never counted) - these
    are "standins": never newly scored (a standin always already has a
    current-rubric score), present purely so the public ranked list
    can still show up to 200 RATED companies when the scored set itself
    contains WAITING rows. A WAITING candidate itself is NEVER a
    standin - only "rated" candidates qualify.

    Returns {"counted": [row, ...], "set_aside": [(row, status), ...],
    "standins": [row, ...], "short": bool}. "counted" is Value Score
    descending (merit_ordered's own order, filtered) - a WAITING
    candidate counts and keeps its place exactly where it already sat
    in merit order, so no replacement is ever pulled into THE SCORED
    SET for it (standins are a separate, additional set, never a
    replacement inside `counted` itself). "short" is True when fewer
    than POOL_SIZE candidates were counted within the walked window -
    the caller logs the WARNING line for that case; "short" is about
    the SCORED SET only and is never affected by how many standins
    were found (a standin can never make up for an unfilled scored-set
    slot - that would mean scoring something, which standins never
    do)."""
    latest = top100_store.latest_scores_for_model(model, RUBRIC_VERSION)
    failures = top100_store.score_failures_for_model(model, RUBRIC_VERSION)
    counted = []
    set_aside = []
    limit = POOL_SIZE + TOP200_BACKFILL_MAX
    walked = 0
    for row in merit_ordered[:limit]:
        walked += 1
        status = company_status(latest.get(row["ticker"]), failures.get(row["ticker"]))
        if status in ("unrated_model", "unrated_failed"):
            set_aside.append((row, status))
            continue
        counted.append(row)
        if len(counted) >= POOL_SIZE:
            break

    waiting_count = sum(
        1 for row in counted
        if company_status(latest.get(row["ticker"]), failures.get(row["ticker"])) == "waiting"
    )
    counted_tickers = {r["ticker"] for r in counted}
    set_aside_tickers = {r["ticker"] for r, _status in set_aside}
    standins = _standins_from_remaining(
        merit_ordered[walked:limit], counted_tickers, set_aside_tickers, waiting_count, model=model)
    return {
        "counted": counted, "set_aside": set_aside, "standins": standins,
        "short": len(counted) < POOL_SIZE,
    }


def _standins_from_remaining(remaining_candidates, already_tickers, set_aside_tickers, waiting_count,
                              model=MODEL_TOP100):
    """A3 amendment of instruction_top200_amendments_and_currency_view.md
    (5 Oct 2026, Director-directed) - the shared stand-in walk, used by
    _apply_backfill() for the main 200 AND, identically, by select_
    top100_pool()'s own ASX-extension block ("the same rule applies to
    the Top 20 tabs and the ASX extension"). Walks `remaining_
    candidates` (already in the right merit order - the tail of
    whatever list the caller was walking) and returns up to
    `waiting_count` rows that are ALREADY RATED, skipping any ticker
    already in `already_tickers`/`set_aside_tickers` and any candidate
    that is UNRATED_MODEL/UNRATED_FAILED/WAITING - a standin is never
    newly scored, never a WAITING candidate, and never re-adds anyone
    to the caller's own set-aside bookkeeping (that stays the caller's
    responsibility, recorded once, during its own primary walk)."""
    if waiting_count <= 0:
        return []
    latest = top100_store.latest_scores_for_model(model, RUBRIC_VERSION)
    failures = top100_store.score_failures_for_model(model, RUBRIC_VERSION)
    standins = []
    for row in remaining_candidates:
        if len(standins) >= waiting_count:
            break
        ticker = row["ticker"]
        if ticker in already_tickers or ticker in set_aside_tickers:
            continue
        if company_status(latest.get(ticker), failures.get(ticker)) == "rated":
            standins.append(row)
    return standins


def _backfill_summary(merit_ordered, backfill, model=MODEL_TOP100):
    """Shared by the live backfill log line, the switch-OFF dry-run
    line, and the owner preview panel - {"set_aside_model": [...],
    "set_aside_failed": [...], "pulled_in": [...], "new_to_score": int,
    "est_cost_usd": float, "standin_tickers": [...], "scored_set_rated":
    int, "scored_set_waiting": int, "public_list_count": int}.
    "pulled_in": tickers in backfill["counted"] that were NOT among
    the first POOL_SIZE positions of `merit_ordered` (i.e. genuinely
    brought in to fill a gap left by a set-aside candidate).
    "new_to_score": how many of those pulled-in tickers are WAITING
    (never scored at all yet under this rubric - the ones a future
    batch will actually have to pay for); est_cost_usd estimates
    scoring exactly those at the current per-company batch rate.

    A3 amendment of instruction_top200_amendments_and_currency_view.md
    (5 Oct 2026, Director-directed): "standin_tickers" is backfill[
    "standins"]'s own tickers, sorted; "scored_set_rated"/"scored_set_
    waiting" are R/W from the task's own "public list would show P of
    200 (scored-set rated R + stand-ins S)" wording; "public_list_
    count" is R + len(standins) - the actual number of RATED companies
    the public page would show (never more than POOL_SIZE, since
    R <= POOL_SIZE - waiting_count and standins is capped at exactly
    waiting_count by _apply_backfill() itself)."""
    baseline_tickers = {r["ticker"] for r in merit_ordered[:POOL_SIZE]}
    counted_tickers = {r["ticker"] for r in backfill["counted"]}
    pulled_in = sorted(counted_tickers - baseline_tickers)
    set_aside_model = sorted(row["ticker"] for row, status in backfill["set_aside"] if status == "unrated_model")
    set_aside_failed = sorted(row["ticker"] for row, status in backfill["set_aside"] if status == "unrated_failed")
    latest = top100_store.latest_scores_for_model(model, RUBRIC_VERSION)
    failures = top100_store.score_failures_for_model(model, RUBRIC_VERSION)
    pulled_in_rows_by_ticker = {r["ticker"]: r for r in backfill["counted"] if r["ticker"] in pulled_in}
    new_to_score = [
        t for t in pulled_in
        if company_status(latest.get(t), failures.get(t)) == "waiting"
    ]
    new_to_score_rows = [pulled_in_rows_by_ticker[t] for t in new_to_score]
    est_cost_usd = estimate_degenerate_resend_cost_usd(new_to_score_rows) if new_to_score_rows else 0.0

    scored_set_waiting = sum(
        1 for row in backfill["counted"]
        if company_status(latest.get(row["ticker"]), failures.get(row["ticker"])) == "waiting"
    )
    scored_set_rated = len(backfill["counted"]) - scored_set_waiting
    standin_tickers = sorted(r["ticker"] for r in backfill["standins"])
    return {
        "set_aside_model": set_aside_model, "set_aside_failed": set_aside_failed,
        "pulled_in": pulled_in, "new_to_score": len(new_to_score), "est_cost_usd": est_cost_usd,
        "standin_tickers": standin_tickers,
        "scored_set_rated": scored_set_rated, "scored_set_waiting": scored_set_waiting,
        "public_list_count": scored_set_rated + len(standin_tickers),
    }


def backfill_preview(log=print, model=MODEL_TOP100):
    """COMMIT 3's own "Top 200 backfill preview" (owner-only Admin
    panel, work with the switch OFF) - computes EXACTLY what tonight's
    selection would look like with TOP200_BACKFILL_LIVE=1, from the
    SAME merit-ordered candidate list select_top100_pool() itself
    would build (_build_best_by_ticker(), read-only - reads already-
    saved scan files, no network call), WITHOUT ever calling top100_
    store.save_pool()/update_pool_presence()/_fill_missing_sectors() -
    no write, no live yfinance call, no batch triggered. Ignores the
    actual env var entirely (this is a preview of the ON state,
    regardless of which state is really active) - top100_render.py's
    preview button is the only caller.

    Returns {"pool": [...], "extension": [...], "set_aside": [...],
    "summary": {...}}. "pool"/"extension"/"set_aside" rows are each
    pre-enriched with "score_row"/"composite"/"score_failure" (shaped
    exactly like top100_render._enrich_rows()' own output, minus the
    previous-rubric fallback that function also applies - a minor,
    display-only simplification: right after a rubric bump every
    pooled row already shows the AWAITING/fallback chip via the real
    page regardless) so the Admin panel can feed them straight into
    the same row-rendering functions the real public page uses, with
    no duplicated rendering logic. "extension" mirrors today's ASX-
    extension rule unchanged (never backfilled itself - see COMMIT 3's
    own "Top 20 tabs" report section for why), filtered to exclude any
    ticker this preview set aside."""
    best_by_ticker, _eligible_count = _build_best_by_ticker(log=log)
    merit_ordered = sorted(best_by_ticker.values(), key=lambda r: r["value_score"], reverse=True)
    backfill = _apply_backfill(merit_ordered, model=model)
    summary = _backfill_summary(merit_ordered, backfill, model=model)
    # A3 amendment of instruction_top200_amendments_and_currency_view.md
    # (5 Oct 2026, Director-directed): standins are ordinary pool
    # members in the preview too, exactly mirroring select_top100_
    # pool()'s own switch-ON branch, so the Admin preview can never
    # disagree with what the live switch would actually produce.
    pool = backfill["counted"] + backfill["standins"]
    pool_tickers = {r["ticker"] for r in pool}
    set_aside_rows = []
    for row, status in backfill["set_aside"]:
        row["backfill_set_aside_status"] = status
        set_aside_rows.append(row)
    set_aside_tickers = {r["ticker"] for r in set_aside_rows}

    au_in_pool = sum(1 for t in pool_tickers if t.endswith(".AX"))
    extension = []
    if au_in_pool < TOP20_AU_TARGET:
        needed = TOP20_AU_TARGET - au_in_pool
        _au_sorted_candidates = sorted(
            (r for t, r in best_by_ticker.items()
             if t.endswith(".AX") and t not in pool_tickers and t not in set_aside_tickers),
            key=lambda r: r["value_score"], reverse=True,
        )
        extension = _au_sorted_candidates[:needed]
        # A3 amendment: the same stand-in rule as the ASX extension
        # block in select_top100_pool() itself.
        _preview_latest = top100_store.latest_scores_for_model(model, RUBRIC_VERSION)
        _preview_failures = top100_store.score_failures_for_model(model, RUBRIC_VERSION)
        _au_waiting_count = sum(
            1 for row in extension
            if company_status(_preview_latest.get(row["ticker"]), _preview_failures.get(row["ticker"])) == "waiting"
        )
        _au_already = {r["ticker"] for r in extension}
        extension = extension + _standins_from_remaining(
            _au_sorted_candidates[needed:], _au_already, set_aside_tickers, _au_waiting_count, model=model)

    latest = top100_store.latest_scores_for_model(model, RUBRIC_VERSION)
    failures = top100_store.score_failures_for_model(model, RUBRIC_VERSION)

    def _enrich(row):
        score_row = latest.get(row["ticker"])
        return {**row, "score_row": score_row, "composite": composite_score(score_row),
                "is_fallback_score": False, "score_failure": failures.get(row["ticker"])}

    return {
        "pool": [_enrich(r) for r in pool],
        "extension": [_enrich(r) for r in extension],
        "set_aside": set_aside_rows,
        "summary": summary,
    }


# -----------------------------------------------------------------
# COMMIT 4 of instruction_top200_unrated_and_blank_replies_combined.md
# (5 Oct 2026, Director-directed, switch TOP200_SCHEMA_MODE, unset by
# default = "legacy"): "ticker"'s own position in the per-company
# request schema moves to first when the switch is set to
# "ticker_first" - nothing else about the schema, prompt, model, or
# request shape changes. See _company_item_schema()'s own docstring
# for the one-line diff this produces.
# -----------------------------------------------------------------

_SCHEMA_MODE_WARNED_VALUES = set()


def top200_schema_mode(log=print):
    """"legacy" (the default) or "ticker_first". Same unset/""=legacy
    convention as every other switch in this module, but unlike is_
    backfill_live()'s plain on/off this one reads a STRING value with
    a third case: any value that is neither "" (unset) nor
    "ticker_first" is treated as "legacy" (the safe fallback) and
    logged as a WARNING exactly once per distinct unrecognized value
    for the life of this process - not re-logged on every call, since
    this is read once per batch submission and re-logging on every one
    of a night's several submit calls would spam the Railway log for a
    single typo that only needs reporting once.

    Re-read fresh from the environment on every call (same as every
    other switch here) - EXCEPT at the two points that must not drift
    mid-batch: submit_nightly_batch() reads it once per submission and
    stores the result per-pack in custom_id_map; poll_and_ingest_
    batch() reads it back from that stored value at ingest time rather
    than calling this function again, so a batch already in flight
    always ingests under the mode it was actually submitted with, even
    if Andrew flips the switch before that batch is polled. Claude Code
    never sets this env var - the Director does, on Railway."""
    raw = (os.environ.get("TOP200_SCHEMA_MODE") or "").strip()
    if raw in ("", "ticker_first"):
        return raw or "legacy"
    if raw not in _SCHEMA_MODE_WARNED_VALUES:
        _SCHEMA_MODE_WARNED_VALUES.add(raw)
        log(f"WARNING: TOP200_SCHEMA_MODE={raw!r} not recognized (expected unset or "
            f"\"ticker_first\") - treating as \"legacy\" for this and all further calls "
            f"with this value.")
    return "legacy"


def schema_mode_breakdown(model=MODEL_TOP100):
    """Admin Dashboard, display only (COMMIT 4 of instruction_top200_
    unrated_and_blank_replies_combined.md, 5 Oct 2026): {schema_mode:
    {"count", "not_rated_share", "mean_composite"}, ...} over every
    CURRENTLY STORED score row under (model, RUBRIC_VERSION) - a row
    saved before this commit shipped has schema_mode None, grouped
    here under "legacy" (same convention as top100_store.schema_mode_
    for_batch()). "mean_composite" reuses composite_score() (the same
    0-100 Research Score the public page shows) averaged over the
    group's RATED rows only (None for a group with zero rated rows);
    "not_rated_share" is a 0.0-1.0 fraction of the WHOLE group. Pure
    read, no network, no write - purely informational grouping, never
    read by scoring/sorting/selection."""
    latest = top100_store.latest_scores_for_model(model, RUBRIC_VERSION)
    groups = {}
    for row in latest.values():
        mode = row.get("schema_mode") or "legacy"
        groups.setdefault(mode, []).append(row)
    out = {}
    for mode, rows in groups.items():
        composites = [c for c in (composite_score(r) for r in rows) if c is not None]
        not_rated_count = sum(1 for r in rows if r.get("not_rated"))
        out[mode] = {
            "count": len(rows),
            "not_rated_share": (not_rated_count / len(rows)) if rows else 0.0,
            "mean_composite": (sum(composites) / len(composites)) if composites else None,
        }
    return out


# -----------------------------------------------------------------
# COMMIT 5 of instruction_top200_unrated_and_blank_replies_combined.md
# (5 Oct 2026, Director-directed, PART C - "rating on what is known")
# - owner-only, read-only dry run. From stored data only: no network
# call, no new scoring, no write to any score row, no change to
# anything the public sees. Confirmed as buildable per STEP C0.2
# (stored NOT RATED rows DO keep the real dimension scores/
# justifications the model gave for whichever of the ten it actually
# scored - see composite_score()'s own docstring/_parse_one_company()
# for where this is proven).
# -----------------------------------------------------------------

def _k_scored(score_row):
    """Number of dimensions (0-10) with a real (non-null) score on a
    stored row - "10 minus the zeros" in the task's own words."""
    dims = score_row.get("dims") or {}
    return sum(1 for key in DIMENSION_KEYS if (dims.get(key) or {}).get("score") is not None)


def _unscored_dimension_keys(score_row):
    """The dimension keys with no real score on a stored row - for
    Table 3's "which dimensions are unscored" column."""
    dims = score_row.get("dims") or {}
    return [key for key in DIMENSION_KEYS if (dims.get(key) or {}).get("score") is None]


def _composite_score_gap_as_middle(dims, gap_score=3):
    """Method 2 ("gap counted as the middle score", COMMIT 5's own
    task wording): each dimension with no real score is counted as 3
    (the middle of the 1-5 scale) instead of being left out and the
    remaining weight rescaled (Method 1 - see _composite_score_from_
    dims()). Always returns a 0-100 number (every dimension counts
    toward the full DIMENSION_WEIGHT_TOTAL regardless of how many were
    actually scored) - never None, unlike Method 1."""
    total = 0.0
    for key in DIMENSION_KEYS:
        score = (dims.get(key) or {}).get("score")
        if score is None:
            score = gap_score
        weight = DIMENSION_WEIGHT[key]
        total += weight * (score - 1) / 4.0
    return round(total * (100.0 / DIMENSION_WEIGHT_TOTAL), 2)


def _partial_rating_company_name_map(model=MODEL_TOP100):
    """ticker -> company_name from the CURRENT pool + ASX extension
    only (the one place this engine module already has that string) -
    a ticker scored in the past that has since left the pool entirely
    simply has no name available here; callers fall back to the
    ticker itself in that case. Read-only, no network call."""
    rows = top100_store.current_pool() + top100_store.current_asx_extension()
    return {r["ticker"]: r.get("company_name") for r in rows if r.get("company_name")}


def _partial_rating_top20_tab_membership(model=MODEL_TOP100):
    """{"mixed": [score_row, ...], "au": [...], "us": [...]} - today's
    ACTUAL membership of the three Top 20-shaped tabs (Full 100/200 is
    deliberately excluded - the task's own "Top 20 tabs" wording),
    reproduced via the SAME private render functions the real page
    itself calls (top100_render._enriched_pool()/_enriched_asx_
    extension()/_sort_and_gate_rated()) rather than re-deriving the
    ranking/gating logic a second time - this codebase's own
    precedent for reusing a private-by-convention function from
    outside its module (see CLAUDE.md's own note on api_v1._resolve_
    universe(), and COMMIT 3's backfill_preview()/app.py's own preview
    panel doing the same). Ordered by SORT_RESEARCH (the page's own
    default/first sort option) - sort mode changes ORDER, not which
    rows are RATED, so this is a stable choice of "today's" view for
    a display-only table. Deferred import (function-local, not at
    module level) avoids the circular import: top100_render.py
    already imports this module at ITS own top level."""
    import top100_render
    enriched = top100_render._enriched_pool()
    extension = top100_render._enriched_asx_extension()
    mixed = top100_render._sort_and_gate_rated(enriched, top100_render.SORT_RESEARCH)[:20]
    us = top100_render._sort_and_gate_rated(
        [r for r in enriched if r["currency"] == "USD"], top100_render.SORT_RESEARCH)[:20]
    pool_au = [r for r in enriched if r["currency"] == "AUD"]
    au = top100_render._sort_and_gate_rated(pool_au + extension, top100_render.SORT_RESEARCH)
    return {
        "mixed": [r["score_row"] for r in mixed if r.get("score_row")],
        "us": [r["score_row"] for r in us if r.get("score_row")],
        "au": [r["score_row"] for r in au if r.get("score_row")],
    }


def partial_rating_dry_run(model=MODEL_TOP100):
    """The owner-only "Rating on what is known" dry run - Admin
    Dashboard, read-only, from stored data only. No network call, no
    new scoring, no write to any score row (nothing in this function
    calls any top100_store.save_* function), no change to selection,
    the public page, or the prompt. Every company considered is one
    with a stored row under (model, RUBRIC_VERSION) - top100_store.
    latest_scores_for_model()'s own return value, the exact same read
    _unscored_tickers()/_enriched_pool() already use elsewhere in this
    module, so this dry run can never disagree with them about what
    is currently stored.

    Returns a dict with five tables (named exactly as in the task) plus
    a "summary" for the one log line poll_and_ingest_batch() prints
    after every ingest:

    "table1_by_k": {market: {k: count}} for k in 0..7 - UNRATED_MODEL
    companies only (company_status() == "unrated_model"; UNRATED_
    FAILED is a different end-state, never counted here), by how many
    of their ten dimensions carry a real score.

    "table2_lower_bar": {min_k: {market: {"would_be_rated", "stays_
    unrated"}}} for min_k in (7, 6, 5) - of today's UNRATED_MODEL
    companies, how many would cross a lower k-threshold.

    "table3_landing": one row per UNRATED_MODEL company with k >= 6
    (the task's own "at a minimum of 6" threshold for this table) -
    ticker, company, market, k, unscored_dimensions (keys), method1_
    score/method1_rank, method2_score/method2_rank (rank = place among
    TODAY'S rated companies' own scores under that method, 1 = best),
    lands_top50_method1/lands_top50_method2 (bool), and empty_fields -
    always ["inversion_scenario", "inversion_severity", "current_
    headwind"] for every row here, since _parse_one_company() forces
    exactly those three to None, unconditionally, for every NOT RATED
    row regardless of k (see that function's own docstring) - market_
    structure/one_foot_hurdle/munger_quality/big_wave are NOT forced
    and may well be filled (STEP C0.4), so they are never listed here.

    "table4_existing_gaps": one row per RATED company with exactly 1 or
    2 null dimensions today - ticker, company, market, gaps (1 or 2),
    today_rank (under Method 1 - i.e. its real, already-stored
    composite_score()), method2_rank, and the change (today_rank minus
    method2_rank; positive means Method 2 would move it UP the
    ranking). Andrew needs this because switching everyone to Method 2
    would also move companies that are already rated today, not only
    the newly-rated ones in Table 3.

    "table5_top20_tabs": {"mixed"/"au"/"us": {"members", "below_8"}} -
    of each Top 20-shaped tab's own CURRENT, real membership (see
    _partial_rating_top20_tab_membership()), how many have fewer than
    8 scored dimensions. The Full 100/200 tab is deliberately excluded
    - the task's own "Top 20 tabs" wording."""
    latest = top100_store.latest_scores_for_model(model, RUBRIC_VERSION)
    failures = top100_store.score_failures_for_model(model, RUBRIC_VERSION)
    name_map = _partial_rating_company_name_map(model)

    # Fix (Director, 6 Oct 2026, instruction_portfolio_scoring_and_
    # currency_table.md, PART D STEP D1): this used to iterate over
    # EVERY row latest_scores_for_model() has ever stored, with no join
    # to today's pool - silently including tickers that have since left
    # the Top 200 (and its Australia extension). The owner's own report:
    # the coverage panel (app.py's _render_top200_coverage_panel(),
    # which joins top100_engine.coverage_for_rows() against exactly
    # top100_store.current_pool() + current_asx_extension()) showed 24
    # UNRATED_MODEL (Australia 9, USA 15); this dry run showed 27
    # (10/17) - the 3 extra were aged-out tickers. _pool_tickers below
    # is the SAME join coverage_for_rows() already does, reused here
    # rather than re-derived, so the two panels can never disagree about
    # POPULATION again (they can still legitimately disagree about
    # TIMING if the pool changes between one render and the next, since
    # each is read fresh - see the "Coverage panel unrated: N. This
    # panel: N." line this step adds at the top of the panel itself).
    _pool_tickers = ({row["ticker"] for row in top100_store.current_pool()}
                      | {row["ticker"] for row in top100_store.current_asx_extension()})

    unrated_model_rows = [
        row for row in latest.values()
        if row["ticker"] in _pool_tickers
        and company_status(row, failures.get(row["ticker"])) == "unrated_model"
    ]
    rated_rows = [
        row for row in latest.values()
        if row["ticker"] in _pool_tickers and not row.get("not_rated")
    ]

    # Table 1 - how thin are the unrated.
    table1 = {}
    for row in unrated_model_rows:
        market = peer_context.market_for(row["ticker"])
        k = _k_scored(row)
        table1.setdefault(market, {}).setdefault(k, 0)
        table1[market][k] += 1

    # Table 2 - what a lower bar would do.
    table2 = {}
    for min_k in (7, 6, 5):
        by_market = {}
        for row in unrated_model_rows:
            market = peer_context.market_for(row["ticker"])
            bucket = by_market.setdefault(market, {"would_be_rated": 0, "stays_unrated": 0})
            if _k_scored(row) >= min_k:
                bucket["would_be_rated"] += 1
            else:
                bucket["stays_unrated"] += 1
        table2[min_k] = by_market

    # Baseline for "place among today's rated companies": today's
    # RATED companies' own scores under each method - Method 1 is
    # simply their real, already-stored composite_score(); Method 2 is
    # recomputed for every one of them too (a RATED company can itself
    # have 1-2 gaps, which Method 2 treats differently from Method 1 -
    # see Table 4).
    rated_method1 = [composite_score(row) for row in rated_rows]
    rated_method1 = [c for c in rated_method1 if c is not None]
    rated_method2 = [_composite_score_gap_as_middle(row["dims"]) for row in rated_rows]

    def _rank_among_rated(candidate_score, rated_scores):
        return 1 + sum(1 for s in rated_scores if s > candidate_score)

    # Table 3 - where they would rank (minimum of 6 scored dimensions).
    table3 = []
    for row in unrated_model_rows:
        k = _k_scored(row)
        if k < 6:
            continue
        ticker = row["ticker"]
        method1_score = _composite_score_from_dims(row["dims"])
        method2_score = _composite_score_gap_as_middle(row["dims"])
        method1_rank = _rank_among_rated(method1_score, rated_method1) if method1_score is not None else None
        method2_rank = _rank_among_rated(method2_score, rated_method2)
        table3.append({
            "ticker": ticker,
            "company": name_map.get(ticker, ticker),
            "market": peer_context.market_for(ticker),
            "k": k,
            "unscored_dimensions": _unscored_dimension_keys(row),
            "method1_score": method1_score, "method1_rank": method1_rank,
            "method2_score": method2_score, "method2_rank": method2_rank,
            "lands_top50_method1": method1_rank is not None and method1_rank <= 50,
            "lands_top50_method2": method2_rank <= 50,
            "empty_fields": ["inversion_scenario", "inversion_severity", "current_headwind"],
        })

    # Table 4 - effect on companies already rated (1 or 2 gaps today).
    table4 = []
    for row in rated_rows:
        k = _k_scored(row)
        gaps = 10 - k
        if gaps not in (1, 2):
            continue
        ticker = row["ticker"]
        today_score = composite_score(row)
        method2_score = _composite_score_gap_as_middle(row["dims"])
        today_rank = _rank_among_rated(today_score, rated_method1)
        method2_rank = _rank_among_rated(method2_score, rated_method2)
        table4.append({
            "ticker": ticker,
            "company": name_map.get(ticker, ticker),
            "market": peer_context.market_for(ticker),
            "gaps": gaps,
            "today_rank": today_rank, "method2_rank": method2_rank,
            "change": today_rank - method2_rank,
        })

    # Table 5 - Top 20 tabs.
    table5 = {}
    for tab_name, members in _partial_rating_top20_tab_membership(model).items():
        table5[tab_name] = {
            "members": len(members),
            "below_8": sum(1 for row in members if _k_scored(row) < 8),
        }

    summary = {
        "unrated_model": len(unrated_model_rows),
        "would_be_rated_min7": sum(b["would_be_rated"] for b in table2[7].values()),
        "would_be_rated_min6": sum(b["would_be_rated"] for b in table2[6].values()),
        "would_be_rated_min5": sum(b["would_be_rated"] for b in table2[5].values()),
        "landing_top50_min6_method1": sum(1 for r in table3 if r["lands_top50_method1"]),
        "landing_top50_min6_method2": sum(1 for r in table3 if r["lands_top50_method2"]),
    }
    return {
        "table1_by_k": table1, "table2_lower_bar": table2, "table3_landing": table3,
        "table4_existing_gaps": table4, "table5_top20_tabs": table5, "summary": summary,
    }


def _log_partial_rating_dry_run(log=print, model=MODEL_TOP100):
    """The task's own one log line, called after every ingest (COMMIT
    5's own requirement) - never raises (same "must never stop the
    site serving" discipline as every other post-ingest log call in
    poll_and_ingest_batch())."""
    try:
        result = partial_rating_dry_run(model=model)
    except Exception as e:
        log(f"[top100] partial-rating dry run failed, skipping: {e}")
        return
    s = result["summary"]
    log(f"[top100] partial-rating dry run: unrated_model {s['unrated_model']} | "
        f"would be rated at min 7: {s['would_be_rated_min7']} | "
        f"min 6: {s['would_be_rated_min6']} | min 5: {s['would_be_rated_min5']} | "
        f"landing in top 50 at min 6: method1 {s['landing_top50_min6_method1']}, "
        f"method2 {s['landing_top50_min6_method2']}")


def coverage_for_rows(rows, model=MODEL_TOP100):
    """The status + market of every row in `rows` (pool + ASX
    extension, or whatever selection the caller wants a coverage
    picture of) - {"per_ticker": {ticker: {"market", "status"}},
    "by_market": {market: {"rated", "unrated_model", "unrated_failed",
    "waiting"}}}. Pure/read-only - one bulk read of latest_scores_for_
    model()/score_failures_for_model(), no per-ticker query, no
    network call, same shape of read top100_render._enriched_pool()
    already does."""
    latest = top100_store.latest_scores_for_model(model, RUBRIC_VERSION)
    failures = top100_store.score_failures_for_model(model, RUBRIC_VERSION)
    per_ticker = {}
    by_market = {}
    for row in rows:
        ticker = row["ticker"]
        market = peer_context.market_for(ticker)
        status = company_status(latest.get(ticker), failures.get(ticker))
        per_ticker[ticker] = {"market": market, "status": status}
        bucket = by_market.setdefault(
            market, {"rated": 0, "unrated_model": 0, "unrated_failed": 0, "waiting": 0})
        bucket[status] += 1
    return {"per_ticker": per_ticker, "by_market": by_market}


def _log_coverage(rows, log=print, model=MODEL_TOP100):
    """The task's own two log lines, called after every selection and
    after every ingest. Never raises - coverage is a reporting side
    effect, not a gate on selection/scoring succeeding."""
    try:
        coverage = coverage_for_rows(rows, model=model)
        by_market = coverage["by_market"]
        total_rated = sum(b["rated"] for b in by_market.values())
        total_unrated_model = sum(b["unrated_model"] for b in by_market.values())
        total_unrated_failed = sum(b["unrated_failed"] for b in by_market.values())
        total_waiting = sum(b["waiting"] for b in by_market.values())
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        per_market_bits = " | ".join(
            f"{market} {b['rated']}/{b['unrated_model']}/{b['unrated_failed']}/{b['waiting']}"
            for market, b in sorted(by_market.items())
        )
        log(f"[top100] coverage {today}: selected {len(rows)} | rated {total_rated} | "
            f"unrated_model {total_unrated_model} | unrated_failed {total_unrated_failed} | "
            f"waiting {total_waiting} || {per_market_bits}")
        for market, b in sorted(by_market.items()):
            resolved = b["rated"] + b["unrated_model"] + b["unrated_failed"]
            if resolved < UNRATED_SHARE_WARNING_MIN_RESOLVED:
                continue
            unrated_share = 100.0 * (b["unrated_model"] + b["unrated_failed"]) / resolved
            if unrated_share > UNRATED_SHARE_WARNING_PCT:
                log(f"[top100] coverage WARNING: {market} unrated share "
                    f"{unrated_share:.1f}% ({b['unrated_model'] + b['unrated_failed']} "
                    f"of {resolved} resolved)")
        return coverage
    except Exception as e:
        log(f"[top100] coverage logging failed (non-fatal): {e}")
        return None


def zeros_breakdown_for_rows(rows, model=MODEL_TOP100):
    """The "Addition" from the same commit - how many of UNRATED_MODEL's
    own zero dimensions land on each of the ten DIMENSION_KEYS, per
    market; and, among RATED companies, how many carry exactly one or
    exactly two zero dimensions (one and two are counted separately -
    "one or two further gaps" is the owner's own point, and a RATED
    company can never have three or more, by the NOT RATED rule
    itself). {"by_market": {market: {"unrated_model_by_dimension":
    {dim_key: count}, "rated_with_1_zero": N, "rated_with_2_zeros":
    N}}}."""
    latest = top100_store.latest_scores_for_model(model, RUBRIC_VERSION)
    failures = top100_store.score_failures_for_model(model, RUBRIC_VERSION)
    by_market = {}
    for row in rows:
        ticker = row["ticker"]
        score_row = latest.get(ticker)
        status = company_status(score_row, failures.get(ticker))
        if status not in ("unrated_model", "rated"):
            continue
        market = peer_context.market_for(ticker)
        bucket = by_market.setdefault(market, {
            "unrated_model_by_dimension": {k: 0 for k in DIMENSION_KEYS},
            "rated_with_1_zero": 0, "rated_with_2_zeros": 0,
        })
        zero_keys = [k for k in DIMENSION_KEYS if (score_row["dims"].get(k) or {}).get("score") is None]
        if status == "unrated_model":
            for k in zero_keys:
                bucket["unrated_model_by_dimension"][k] += 1
        else:  # rated
            if len(zero_keys) == 1:
                bucket["rated_with_1_zero"] += 1
            elif len(zero_keys) == 2:
                bucket["rated_with_2_zeros"] += 1
    return {"by_market": by_market}


def _log_zeros_breakdown(rows, log=print, model=MODEL_TOP100):
    """The task's own zeros log line, called after each ingest only.
    Never raises - same non-fatal reporting status as _log_coverage()."""
    try:
        breakdown = zeros_breakdown_for_rows(rows, model=model)
        totals = {k: 0 for k in DIMENSION_KEYS}
        for b in breakdown["by_market"].values():
            for k, c in b["unrated_model_by_dimension"].items():
                totals[k] += c
        coverage = coverage_for_rows(rows, model=model)
        unrated_model_total = sum(
            b["unrated_model"] for b in coverage["by_market"].values())
        by_dim_bits = ", ".join(f"{k} {totals[k]}" for k in DIMENSION_KEYS)
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        log(f"[top100] zeros {today}: unrated_model {unrated_model_total} | "
            f"by dimension: {by_dim_bits}")
        return breakdown
    except Exception as e:
        log(f"[top100] zeros logging failed (non-fatal): {e}")
        return None


def _pending_batch_tickers():
    """Tickers currently packed into the one in-flight Batch API
    submission (top100_store.get_batch_state()), or an empty set if no
    batch is pending. custom_id_map is keyed by per-pack custom_id, each
    holding an "entrants" dict keyed by ticker (see submit_nightly_
    batch()'s own custom_id_map construction) - this flattens that
    structure for a single ticker membership check."""
    state = top100_store.get_batch_state()
    if not state:
        return set()
    tickers = set()
    for pack in (state.get("custom_id_map") or {}).values():
        tickers.update((pack.get("entrants") or {}).keys())
    return tickers


def waiting_detail_for_rows(rows, model=MODEL_TOP100):
    """COMMIT 1 of instruction_top200_unrated_and_blank_replies_combined.md
    - Admin-only detail for every WAITING ticker among `rows` (per
    company_status()'s own WAITING case: no score row, and either no
    failure on record or a failure that isn't yet exhausted per
    _failure_exhausted()). One dict per waiting ticker:
      {"ticker", "market", "reason", "attempts", "next_attempt_at"}
    `reason` is the first of, in this order:
      - "pending batch": the ticker is packed into the one currently
        in-flight Batch API submission (get_batch_state()).
      - "retrying": a failure row exists for this (ticker, model,
        RUBRIC_VERSION) and isn't exhausted yet - attempts/next_attempt_at
        come from that row's own attempts/failed_at + TOP100_FAILURE_
        RETRY_HOURS.
      - "deferred (newcomer)": _true_newcomer_gated() holds - a true
        newcomer that hasn't yet held its pool slot for NEWCOMER_
        PERSISTENCE_NIGHTS consecutive nightly selections.
      - "awaiting first score": none of the above - a genuinely new or
        rubric-bumped entrant that the next submission will pick up.
    attempts/next_attempt_at are 0/None unless the "retrying" reason
    applies. Read-only - no network call, no write."""
    latest = top100_store.latest_scores_for_model(model, RUBRIC_VERSION)
    failures = top100_store.score_failures_for_model(model, RUBRIC_VERSION)
    waiting_rows = [
        row for row in rows
        if company_status(latest.get(row["ticker"]), failures.get(row["ticker"])) == "waiting"
    ]
    pending_batch_tickers = _pending_batch_tickers()
    presence_map = top100_store.pool_presence_map([row["ticker"] for row in waiting_rows])
    out = []
    for row in waiting_rows:
        ticker = row["ticker"]
        market = peer_context.market_for(ticker)
        failure = failures.get(ticker)
        attempts = 0
        next_attempt_at = None
        if ticker in pending_batch_tickers:
            reason = "pending batch"
        elif failure is not None:
            reason = "retrying"
            attempts = failure.get("attempts") or 0
            failed_at = failure.get("failed_at")
            if failed_at:
                try:
                    failed_dt = datetime.fromisoformat(failed_at)
                    if failed_dt.tzinfo is None:
                        failed_dt = failed_dt.replace(tzinfo=timezone.utc)
                    next_attempt_at = (
                        failed_dt + timedelta(hours=TOP100_FAILURE_RETRY_HOURS)
                    ).isoformat()
                except Exception:
                    next_attempt_at = None
        elif _true_newcomer_gated(ticker, model, presence_map):
            reason = "deferred (newcomer)"
        else:
            reason = "awaiting first score"
        out.append({
            "ticker": ticker, "market": market, "reason": reason,
            "attempts": attempts, "next_attempt_at": next_attempt_at,
        })
    return out


def unrated_failed_detail_for_rows(rows, model=MODEL_TOP100):
    """COMMIT 1 of instruction_top200_unrated_and_blank_replies_combined.md
    - Admin-only detail for every UNRATED_FAILED ticker among `rows`:
    {"ticker", "market", "large_company"}, where large_company is True
    when the ticker is a member of any LARGE_COMPANY_UNIVERSES universe
    (per the task's own "large well-known company under 'Could not be
    rated' is flagged to Andrew in Admin" rule). Read-only."""
    latest = top100_store.latest_scores_for_model(model, RUBRIC_VERSION)
    failures = top100_store.score_failures_for_model(model, RUBRIC_VERSION)
    large_tickers = _large_company_tickers()
    out = []
    for row in rows:
        ticker = row["ticker"]
        status = company_status(latest.get(ticker), failures.get(ticker))
        if status != "unrated_failed":
            continue
        out.append({
            "ticker": ticker,
            "market": peer_context.market_for(ticker),
            "large_company": ticker in large_tickers,
        })
    return out


def refresh_all(log=print):
    """Owner-only "refresh all" button - re-selects the pool, then
    submits EVERY pooled company (not just entrants _unscored_tickers()
    would flag) via submit_nightly_batch(force=True).

    Results-driven Top 100 refresh (27 Sep 2026): each entrant now gets
    its own natural score key (RP<most_recent_quarter>/D<today>, see
    submit_nightly_batch()'s own force= docstring), computed there - this
    function no longer passes a `quarter` at all (submit_nightly_batch()
    dropped that parameter). An unchanged ticker naturally reproduces its
    existing key and upsert-overwrites it, which is the explicitly
    accepted behaviour for this EXPLICIT owner-initiated action (unlike
    the automatic nightly path, which never does this for a rule-(c)
    entrant). save_score()'s own per-ticker UPSERT already only ever
    touches a ticker that actually SUCCEEDED in the batch, so this loses
    no failure-safety. Returns the submitted batch id, or None.

    ASX extension (Top 20 Australia guaranteed-twenty, 25 Sep 2026):
    re-selecting also re-selects the extension (select_top100_pool()'s
    own job), so this explicitly folds top100_store.current_asx_
    extension() into the submitted pool too - "Refresh all" refreshes
    Australia's guaranteed twenty exactly as thoroughly as the global
    100, not just the tickers that happen to already be in the 100."""
    pool = select_top100_pool(log=log)
    extension = top100_store.current_asx_extension()
    return submit_nightly_batch(pool=pool + extension, model=MODEL_TOP100, log=log, force=True)


# -----------------------------------------------------------------
# Composite (Python-computed, never by the model).
# -----------------------------------------------------------------

def composite_score(score_row):
    """The 0-100 "Research Score" (the page's own public name for this
    number) for one company - None for a NOT RATED company (score_row
    is None, or score_row["not_rated"] is True).

    Top 100 Research Score rework (25 Sep 2026, owner-approved mock,
    "top100_research_ranking_mock_2.html"): PURE Claude analytical
    judgment now - no margin-of-safety component. Previously this
    function also blended WEIGHT_RETURN% (17) of MOS, normalised
    across the pool, into the total; that's removed. MOS already
    selects which 100 companies are even in the pool (via the Value
    Score), so pricing it into the Research Score a second time
    double-counted valuation and let day-to-day price wobble distort
    what was meant to be a pure analytical ranking. MOS stays
    displayed on every row as information, and is now its own sort
    option (top100_render.py's four-way sort bar, "Best value today")
    rather than a scoring input. WEIGHT_RETURN itself is untouched
    (still asserted to sum with DIMENSIONS to 100 above) - it's simply
    no longer read by this function.

    composite = sum over each NON-NULL AI dimension of
                weight_i * (score_i - 1) / 4
              RESCALED (x 100 / available_weight) so the total sits on
              a 0-100 scale while every dimension's weight keeps its
              exact relative proportion - a fully-scored company (all
              ten dimensions non-null, available_weight ==
              DIMENSION_WEIGHT_TOTAL == 83) is exactly the task's own
              "multiply the analytical total by 100/83" instruction; a
              company with 1-2 null dimensions (not yet NOT RATED,
              which requires >= NOT_RATED_MIN_NULLS null) keeps the
              same "rescale to fill the full weight" treatment this
              formula always gave a partially-scored company, now
              carried through to the 100-point scale instead of
              stopping at 83.

    (score - 1) / 4 maps the 1-5 scale onto 0..1 linearly (1 -> 0.0,
    5 -> 1.0) - a true fraction-of-the-dimension's-own-weight, not the
    score/5 a first glance might suggest (which would map a 1 to 20%
    of the weight rather than 0%, silently rewarding the worst
    possible reading on a dimension).

    COMMIT 5 of instruction_top200_unrated_and_blank_replies_combined.md
    (5 Oct 2026, Director-directed, STEP C0.1): the actual rescale
    arithmetic now lives in _composite_score_from_dims() below - this
    function is unchanged for every existing caller (same not_rated
    short-circuit, same return value), it just delegates the number-
    crunching so the owner-only dry run can apply the IDENTICAL
    formula ("Method 1, today's method extended") to a stored NOT
    RATED row's own dims, which this function itself always refuses."""
    if score_row is None or score_row.get("not_rated"):
        return None
    return _composite_score_from_dims(score_row["dims"])


def _composite_score_from_dims(dims):
    """The leave-out-and-rescale arithmetic composite_score() itself
    applies, extracted so COMMIT 5's dry run ("Method 1 - today's
    method extended") can run the exact same formula on a stored NOT
    RATED row's own dims - something composite_score() itself always
    refuses (see its own not_rated short-circuit). None when zero
    dimensions are scored (nothing to rescale)."""
    available_weight = 0.0
    ai_component = 0.0
    for key in DIMENSION_KEYS:
        dim = dims.get(key) or {}
        score = dim.get("score")
        if score is None:
            continue
        weight = DIMENSION_WEIGHT[key]
        available_weight += weight
        ai_component += weight * (score - 1) / 4.0
    if available_weight <= 0:
        return None
    return round(ai_component * (100.0 / available_weight), 2)
