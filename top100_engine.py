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

  1. SELECTION (select_top100_pool) - merges every saved universe's
     scan rows (scan_store.list_saved_universes(), "imported" and any
     currently-flagged/non-trading ticker excluded), de-duplicates by
     ticker keeping each ticker's MAXIMUM Value Score ("Long Score" -
     see snapshot_store.py's own _PUBLIC_FIELD_MAP for why that's the
     same number the rest of the site calls "Value Score"), keeps the
     top 100, and persists via top100_store.save_pool().

  2. SCORING (submit_nightly_batch / poll_and_ingest_batch) - a TWO-
     PHASE Batch API flow, not a single blocking call: the Batches API
     can take up to 24h to complete (most within 1h), and a nightly
     scheduler job must never block a scheduler thread anywhere near
     that long (every _run_* job in scheduler_engine.py is expected to
     return in seconds to minutes). So:
       - submit_nightly_batch(): called when there's no batch already
         in flight (top100_store.get_batch_state() is None) - selects
         up to MAX_NIGHTLY_SCORES unscored (ticker, quarter, model)
         entrants, submits ONE Batches API call for all of them, and
         persists the batch id + custom_id->ticker map, then returns
         immediately. No scores are written yet.
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
import re
from datetime import datetime, timezone

import scan_store
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

POOL_SIZE = 100

# Top 20 Australia guaranteed-twenty (25 Sep 2026, owner-approved mock,
# "top20_australia_extended_mock.html") - the ASX EXTENSION's own target
# count. select_top100_pool() tops up rated Australians to this many
# whenever the global pool alone has fewer.
TOP20_AU_TARGET = 20


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


def select_top100_pool(log=print):
    """Merges every saved universe's scan rows (scan_store.
    list_saved_universes(), "imported" excluded), de-duplicates by
    ticker keeping each ticker's MAXIMUM "Long Score" (Value Score),
    excludes any currently-flagged/non-trading row, keeps the top
    POOL_SIZE by Value Score, and persists the result via top100_
    store.save_pool(as_of=today's UTC date). Returns the saved GLOBAL
    pool only (unchanged contract - the ASX extension below is never
    part of this return value) - [{"ticker","company_name","universe",
    "value_score","mos_pct","price","intrinsic_value","currency",
    "psychology","sector"}, ...], Value Score descending. Never raises -
    a single bad universe file is skipped (scan_store.load_scan_raw()
    itself already returns None on any read error), and an empty
    result (no saved scans yet) simply persists/returns an empty pool
    rather than crashing.

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
    best_by_ticker = {}
    for universe in scan_store.list_saved_universes():
        if universe == IMPORTED_UNIVERSE:
            continue
        payload = scan_store.load_scan_raw(universe)
        if not payload:
            continue
        for row in payload.get("rows") or []:
            ticker = (row.get("Ticker") or "").strip().upper()
            value_score = row.get("Long Score")
            if not ticker or value_score is None:
                continue
            if _is_flagged_stale(row):
                continue
            prev = best_by_ticker.get(ticker)
            if prev is None or value_score > prev["value_score"]:
                best_by_ticker[ticker] = {
                    "ticker": ticker,
                    "company_name": row.get("Company Name"),
                    "universe": universe,
                    "value_score": value_score,
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
                    # industry mock): the raw scan row's own "Sector"
                    # string, same "carried through for a display-only
                    # tag, never fed into composite_score()" status as
                    # psychology just above - the page refines it with
                    # the Deep Dive's own finer industry string where
                    # cached (top100_render._finer_industry_by_ticker()).
                    "sector": row.get("Sector"),
                }

    pool = sorted(best_by_ticker.values(), key=lambda r: r["value_score"], reverse=True)[:POOL_SIZE]
    as_of = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    pool_tickers = {r["ticker"] for r in pool}
    for row in pool:
        row["asx_extension"] = False

    au_in_pool = sum(1 for t in pool_tickers if t.endswith(".AX"))
    extension = []
    if au_in_pool < TOP20_AU_TARGET:
        needed = TOP20_AU_TARGET - au_in_pool
        au_candidates = sorted(
            (r for t, r in best_by_ticker.items() if t.endswith(".AX") and t not in pool_tickers),
            key=lambda r: r["value_score"], reverse=True,
        )[:needed]
        for row in au_candidates:
            row["asx_extension"] = True
        extension = au_candidates

    top100_store.save_pool(pool + extension, as_of)
    log(f"[top100] selected {len(pool)} companies for {as_of} "
        f"(from {len(best_by_ticker)} deduped candidates across "
        f"{len([u for u in scan_store.list_saved_universes() if u != IMPORTED_UNIVERSE])} universes); "
        f"ASX extension: {len(extension)} added ({au_in_pool} pool Australians -> "
        f"{au_in_pool + len(extension)} total for Top 20 Australia)")
    return pool


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
            reason = "still scanned, but its Value Score fell outside the top 100"
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
RUBRIC_VERSION = "v3"

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
BATCH_DISCOUNT = 0.5

MAX_NIGHTLY_SCORES = 120

_SYSTEM_PROMPT = """You are screening publicly-listed companies for a factual, descriptive "Top 100" quality shortlist on an investing research site. You are given one company's ticker and name. Score it on TEN qualitative dimensions, each as an integer from 1 to 5 - 5 is ALWAYS the good outcome for a long-term holder of the stock, 1 is ALWAYS the bad outcome, on every dimension, no exceptions. Your response format has NO null/blank values anywhere - every field below names the exact SENTINEL value that stands in for "no value" wherever one is needed.

For each dimension also give a ONE-LINE justification, AT MOST ABOUT 25 WORDS, naming the specific source period it is based on (e.g. "FY25 annual report", "Q2 2026 investor call", "the company's own FY24 10-K risk factors section") - or an empty string "" for source_period if the dimension's score is 0 (see the honesty rule).

HONESTY RULE, the single most important instruction in this prompt: output 0 for a dimension's score (not a number 1-5, and never a middle value like 3 to "play it safe") for ANY dimension you do not have confident, specific, public-record knowledge of for THIS company. 0 is not a real score - it is the sentinel meaning "cannot score honestly". Guessing a plausible-sounding score is worse than admitting you don't know - a 0 is the honest answer, a fabricated 3 is not. If three or more of your ten scores end up 0, that is expected and correct for a company with a thin public record - do not distort your other scores to avoid it. This applies especially to MANAGEMENT QUALITY (dimension 8 below): score it 0 freely whenever the people running the company aren't publicly well known - most companies have no public record of their management's integrity, candour or execution track record, and that is the honest, expected answer, not a failure.

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
   Calibration: "PAYX regulatory & legal 4 - payroll complexity only ever increases; each new rule adds compliance demand." "INTU regulatory & legal 2 - IRS Direct File: a live, political, existential threat to TurboTax consumer."

4. CUSTOMER CONCENTRATION.
   5: thousands of small, individually-replaceable customers; no single customer is material to revenue.
   1: one customer is more than roughly 30% of revenue, or a small handful of customers collectively dominate it.
   Calibration: "OCL.AX customer concentration 1 - effectively all revenue is government procurement; the Defence loss was this risk."

5. PRICING POWER & COST PASS-THROUGH (absorbs inflation exposure - see the de-circularisation principle above, never re-score today's margin level).
   5: has repeatedly raised prices above inflation without losing meaningful volume, and can pass through cost increases (including inflationary ones) quickly.
   1: a pure price-taker in a commoditised market with no pass-through mechanism - margins erode directly as costs or inflation rise.

6. ACCOUNTING QUALITY (capitalisation rate + cash conversion).
   5: free cash flow conversion of roughly 80-120% of net income, with minimal capitalisation of what are effectively normal operating costs (e.g. R&D, software development) onto the balance sheet.
   1: aggressive capitalisation of operating-like costs materially inflates reported free cash flow; cash conversion sits far below net income.
   Calibration: "OCL.AX accounting 2 - capitalises 52% of R&D, which inflated screener FCF by 56%."

7. BALANCE SHEET & FIXED CHARGES - financial AND operating rigidity together (see the de-circularisation principle above, never re-score the raw debt-to-equity ratio alone).
   5: net cash or low net debt/EBITDA, high interest coverage, no concentrated near-term refinancing wall, and a flexible (largely variable) cost structure that can flex down in a downturn.
   1: high leverage, thin interest coverage, a concentrated debt maturity wall in the next 1-2 years, and/or a heavy fixed-cost base that cannot flex down when revenue falls.

8. MANAGEMENT QUALITY - the integrity, candour and execution record of the PEOPLE running the company, distinct from the outcome of any one deal. Score 0 freely where the record isn't publicly known (see the honesty rule above) - most companies have no such record.
   5: a long public record of doing what they said they would do - candid in setbacks, disciplined in guidance, no credibility problems.
   1: a credibility problem - a pattern of over-promising, evasive communication, or conduct that has damaged investor trust.

9. CAPITAL ALLOCATION - incremental ROIC on recent major deployments; buybacks vs SBC dilution (see the de-circularisation principle above, never re-score the company's current ROIC level).
   5: a disciplined, accretive incremental-ROIC record on recent major deployments (M&A, capex); buybacks genuinely reduce the share count net of stock-based-compensation dilution; no pattern of value-destroying write-downs.
   1: a pattern of value-destroying acquisitions or repeated impairments, or buybacks that merely offset SBC dilution without shrinking the real share count.
   Calibration: "SEK.AX capital allocation 2 - repeated impairments on Zhaopin, OCC and Brasil Online aren't bad luck; they're the record."

10. REINVESTMENT RUNWAY - can incremental capital still deploy at current returns (see the de-circularisation principle above, never re-score the recent growth rate).
    5: a long runway of high-return reinvestment opportunities still ahead (an expanding or under-penetrated market) at returns well above the cost of capital.
    1: a mature, saturated market with few remaining high-return reinvestment options - excess cash is likely to be misallocated, or simply returned because there is nowhere better to put it.

INVERSION SYNTHESIS - after scoring all ten dimensions, write ONE sentence naming the single most plausible scenario that could seriously damage this company, plus a severity from 1 (minor) to 5 (plausibly breaks the company). This is a separate analytical synthesis, not a dimension score - it has ZERO effect on any of the ten scores above, on this company's ranking, or on its Top-20 eligibility. If three or more of your ten dimension scores are 0 (this company will be marked NOT RATED), output the sentinel values instead - an empty string "" for the inversion scenario and 0 for its severity - do not invent a damaging scenario for a company you don't know well enough to score in the first place.

CURRENT HEADWIND - a separate field from the inversion above, and easy to confuse with it, so read this carefully: the inversion is HYPOTHETICAL (the worst plausible future scenario); the headwind is ACTUAL and PRESENT (why the market is discounting this company right now, as of your knowledge). In at most 40 words, state the actual, present reason the market is discounting this company - the standing headwind (demand, margins, competition, regulation, sentiment), as of your knowledge. This is what IS weighing on the stock, distinct from the inversion's hypothetical worst case. If no clearly identifiable headwind exists, output an empty string "" - never invent one. Same honesty rule as everywhere else in this prompt: a company you don't know a specific, current headwind for gets the empty-string sentinel, not a guessed one. NOT RATED companies (three or more null dimensions) get the empty-string sentinel here too, same as the inversion fields."""


def _user_prompt(ticker, company_name):
    name = company_name or ticker
    return (
        f"Company: {name} (ticker: {ticker})\n\n"
        "Score this company on all ten dimensions per your instructions, "
        "then write the one-sentence inversion synthesis (scenario + severity) "
        "and the current headwind (at most 40 words, or an empty string)."
    )


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


def _response_schema():
    """v2: the ten dimension objects plus the inversion synthesis pair
    (inversion_scenario/inversion_severity) at the top level - no more
    "summary" (v1's "strongest dimension + what to check" note), since
    the page now shows the inversion line in its place (Commit 2's own
    "replacing the what to check note" instruction) and all ten
    justifications directly (the tap-expand detail), leaving no reader
    of the response who still needs a separate summary field.

    v3 (25 Sep 2026): see _dimension_schema()'s own comment - every
    property here is a plain type with a sentinel, zero union types
    anywhere in this schema (the whole point of this rewrite).

    RUBRIC_VERSION v3 (25 Sep 2026, owner-approved mock, "headwind_
    and_currency_risk_mock.html"): added current_headwind, a PLAIN
    string (no union type, no minLength/maxLength - both unsupported
    by structured outputs, same constraint every other field here
    already respects) with the SAME "" -> null sentinel convention as
    inversion_scenario just above - "no clearly identifiable headwind"
    is a permitted, honest answer, never guessed."""
    props = {key: _dimension_schema() for key in DIMENSION_KEYS}
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
    return {
        "type": "object",
        "properties": props,
        "required": DIMENSION_KEYS + ["inversion_scenario", "inversion_severity", "current_headwind"],
        "additionalProperties": False,
    }


def _request_params(ticker, company_name):
    """The exact MessageCreateParamsNonStreaming-shaped dict for one
    company - shared by submit_nightly_batch() (wrapped in a Batches
    Request) and run_single_test_call() (sent directly, for the
    task's own one real API test call)."""
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
        # (added the same day) further reduces the typical case.
        "max_tokens": 4000,
        "system": [{"type": "text", "text": _SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
        "messages": [{"role": "user", "content": _user_prompt(ticker, company_name)}],
        "output_config": {"format": {"type": "json_schema", "schema": _response_schema()}},
    }


def current_quarter(today=None):
    """"2026Q3"-style quarter label - the cache/cadence key (task's own
    "scores persist per (ticker, quarter, model)")."""
    d = today or datetime.now(timezone.utc).date()
    return f"{d.year}Q{(d.month - 1) // 3 + 1}"


def _parse_response_json(text):
    """Parses one company's structured-output JSON text into
    (dims_dict, not_rated, inversion_scenario, inversion_severity,
    current_headwind). `dims_dict`: {key: {"score","justification",
    "source_period"}, ...} for all ten keys. Raises ValueError on
    malformed JSON or a missing dimension - the caller (poll_and_
    ingest_batch) treats that exactly like any other per-ticker
    failure: logged, skipped, prior cache untouched.

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
    outputs schema (_dimension_schema()/_response_schema() above) can
    no longer declare "1-5" via minimum/maximum - Claude's structured-
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
    data = json.loads(text)
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

    return dims, not_rated, inversion_scenario, inversion_severity, current_headwind


# -----------------------------------------------------------------
# Batch submit / poll (two-phase, non-blocking).
# -----------------------------------------------------------------

def _unscored_tickers(pool, quarter, model):
    """Which pooled rows still need an API call for (quarter, model,
    RUBRIC_VERSION) - rubric_version is folded into the cache key here
    (not just at the storage layer) so a rubric bump makes every
    pooled ticker "unscored" again for the new rubric, even though its
    old-rubric row is still sitting in the DB untouched (see
    top100_store's own schema/migration docstring)."""
    already = top100_store.scores_for_quarter_model(quarter, model, RUBRIC_VERSION)
    return [r for r in pool if r["ticker"] not in already]


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
    Returns None if there was nothing to poll, or a summary dict."""
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
    errored_count = 0
    errored_rest_type_counts = {}
    try:
        for result in client.messages.batches.results(state["batch_id"]):
            ticker = custom_id_map.get(result.custom_id)
            if not ticker:
                continue
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
                # line for the rest, counted by (inner) error type.
                failed += 1
                errored_count += 1
                err = getattr(result.result, "error", None)
                err_type = _batch_result_error_type(err)
                if errored_count <= _ERRORED_DETAIL_LIMIT:
                    detail = _serialize_batch_result_error(err)
                    log(f"[top100] {ticker}: batch result errored #{errored_count} - {detail}")
                else:
                    errored_rest_type_counts[err_type] = errored_rest_type_counts.get(err_type, 0) + 1
                continue
            if result.result.type != "succeeded":
                failed += 1
                log(f"[top100] {ticker}: batch result {result.result.type}, skipped")
                continue
            msg = result.result.message
            text = next((b.text for b in msg.content if b.type == "text"), "")
            prompt_params = _request_params(ticker, ticker)
            try:
                dims, not_rated, inversion_scenario, inversion_severity, current_headwind = \
                    _parse_response_json(text)
            except Exception as e:
                failed += 1
                log(f"[top100] {ticker}: could not parse batch result, skipped ({e})")
                continue
            top100_store.save_score(
                ticker=ticker, quarter=state["quarter"], model=state["model"],
                rubric_version=RUBRIC_VERSION, dims=dims, not_rated=not_rated,
                inversion_scenario=inversion_scenario, inversion_severity=inversion_severity,
                current_headwind=current_headwind,
                prompt=json.dumps(prompt_params), raw_response=text,
            )
            saved += 1
            total_input_tokens += getattr(msg.usage, "input_tokens", 0) or 0
            total_output_tokens += getattr(msg.usage, "output_tokens", 0) or 0
    except Exception as e:
        log(f"[top100] batch result retrieval failed partway through: {e}")

    if errored_rest_type_counts:
        breakdown = ", ".join(f"{t}: {c}" for t, c in sorted(errored_rest_type_counts.items()))
        log(
            f"[top100] {sum(errored_rest_type_counts.values())} more errored result(s) "
            f"beyond the first {_ERRORED_DETAIL_LIMIT} shown above, by error type - {breakdown}"
        )

    top100_store.clear_batch_state()
    cost = estimate_batch_cost_usd(total_input_tokens, total_output_tokens)
    log(f"[top100] batch {state['batch_id']} ingested: {saved} scored, {failed} failed - "
        f"{total_input_tokens:,} input + {total_output_tokens:,} output tokens, "
        f"est. ${cost:.4f} (batch-priced)")
    return {"saved": saved, "failed": failed,
            "input_tokens": total_input_tokens, "output_tokens": total_output_tokens,
            "cost_usd": cost}


def submit_nightly_batch(pool=None, quarter=None, model=MODEL_TOP100, log=print, force=False):
    """Phase 2 of every nightly run, called only when poll_and_ingest_
    batch() found nothing in flight (never both submit AND have a
    batch pending - one in-flight batch at a time, top100_store's own
    singleton row). Selects up to MAX_NIGHTLY_SCORES unscored (ticker,
    quarter, model) entrants from `pool` (defaults to top100_store.
    current_pool()), submits ONE Batches API request covering all of
    them, and persists the batch id + custom_id->ticker map. Returns
    None if there was nothing unscored to submit (the common case once
    the pool is fully scored for the quarter - nightly runs then do
    nothing until the quarter rolls over or "refresh all" is used), or
    the submitted batch's id.

    force=True (SMALL FIX, 25 Sep 2026, owner-reported - refresh_all()'s
    own sole caller): submits the WHOLE pool regardless of whether a
    score already exists for (quarter, model) - skips the
    _unscored_tickers() "already scored, skip" filter entirely, but
    still saves under the SAME plain `quarter` key every other caller
    uses (previously refresh_all() achieved "resubmit everyone" by
    tagging the quarter itself as "{quarter}-refresh-{date}", a
    DIFFERENT cache key from the plain quarter every other write/read
    uses - see this function's own git history for the two bugs that
    caused: (1) top100_render.py's _enriched_pool(), "the one place
    every tab reads from", queries top100_store.scores_for_quarter_
    model(top100_engine.current_quarter(), ...) - an EXACT string
    match - so a refresh-tagged score never matched it and never
    appeared on the page at all, contradicting this function's own
    prior docstring claim that the page "naturally prefers the newer
    result"; (2) _unscored_tickers() at the PLAIN quarter key never
    saw a refresh-tagged row as "already scored", so any submission
    later the same day - this module's own hourly-poll auto-resubmit,
    or that night's regular run_nightly() - would re-submit and
    re-score the exact same companies the owner just paid for minutes
    earlier under Refresh all. save_score()'s own per-ticker UPSERT
    already only ever touches a ticker that actually SUCCEEDED in the
    batch (a failed/errored per-ticker result is simply never written -
    see poll_and_ingest_batch()'s own per-result branches), so writing
    directly to the plain quarter key loses none of the "a failed
    refresh never destroys a prior good score" protection the old
    suffix trick was ALSO providing - that protection came from
    save_score()'s own gating, not from the separate cache key.

    ASX extension (Top 20 Australia guaranteed-twenty, 25 Sep 2026):
    when `pool` isn't explicitly passed, the default now ALSO includes
    top100_store.current_asx_extension() - so an ordinary nightly run
    (run_nightly() calls this with no `pool` arg) scores extension
    members exactly like pool members, same rubric/cache keys, no
    special-casing anywhere below this line. A caller that passes its
    own `pool` (refresh_all() does) controls this explicitly instead."""
    pool = (top100_store.current_pool() + top100_store.current_asx_extension()) if pool is None else pool
    quarter = quarter or current_quarter()
    if not pool:
        return None
    entrants = (pool if force else _unscored_tickers(pool, quarter, model))[:MAX_NIGHTLY_SCORES]
    if not entrants:
        log(f"[top100] every pooled company already scored for {quarter}/{model} - nothing to submit")
        return None

    try:
        import anthropic
        from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
        from anthropic.types.messages.batch_create_params import Request
    except ImportError:
        log("[top100] anthropic package not available - skipping tonight's batch")
        return None

    custom_id_map = {}
    requests = []
    for i, row in enumerate(entrants):
        ticker = row["ticker"]
        custom_id = f"t100-{i}-{re.sub(r'[^A-Za-z0-9]', '', ticker)}"
        custom_id_map[custom_id] = ticker
        requests.append(Request(
            custom_id=custom_id,
            params=MessageCreateParamsNonStreaming(**_request_params(ticker, row.get("company_name"))),
        ))

    try:
        client = anthropic.Anthropic()
        batch = client.messages.batches.create(requests=requests)
    except Exception as e:
        log(f"[top100] batch submission failed: {e}")
        return None

    top100_store.save_batch_state(batch.id, quarter, model, custom_id_map)
    log(f"[top100] submitted batch {batch.id}: {len(entrants)} compan{'y' if len(entrants) == 1 else 'ies'} "
        f"for {quarter}/{model}")
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
    via top100_store.save_score() under the CURRENT quarter/model, same
    as a real batch result would, so the test call's own result is
    immediately visible on the page rather than thrown away. Returns
    {"ticker","dims","not_rated","inversion_scenario","inversion_
    severity","current_headwind","input_tokens","output_tokens",
    "cost_usd"} - standard, non-batch pricing (this call does not go
    through the Batches API), reported honestly as such."""
    import anthropic
    client = anthropic.Anthropic()
    params = _request_params(ticker, company_name)
    resp = client.messages.create(**params)
    text = next((b.text for b in resp.content if b.type == "text"), "")
    dims, not_rated, inversion_scenario, inversion_severity, current_headwind = \
        _parse_response_json(text)
    input_tokens = getattr(resp.usage, "input_tokens", 0) or 0
    output_tokens = getattr(resp.usage, "output_tokens", 0) or 0
    cost = (input_tokens / 1_000_000) * TOP100_INPUT_USD_PER_MTOK + \
        (output_tokens / 1_000_000) * TOP100_OUTPUT_USD_PER_MTOK
    quarter = current_quarter()
    top100_store.save_score(
        ticker=ticker, quarter=quarter, model=MODEL_TOP100, rubric_version=RUBRIC_VERSION,
        dims=dims, not_rated=not_rated,
        inversion_scenario=inversion_scenario, inversion_severity=inversion_severity,
        current_headwind=current_headwind,
        prompt=json.dumps(params), raw_response=text,
    )
    return {
        "ticker": ticker, "dims": dims, "not_rated": not_rated,
        "inversion_scenario": inversion_scenario, "inversion_severity": inversion_severity,
        "current_headwind": current_headwind,
        "input_tokens": input_tokens, "output_tokens": output_tokens, "cost_usd": cost,
    }


def estimate_batch_cost_usd(input_tokens, output_tokens):
    """Batch API pricing (BATCH_DISCOUNT, 50% of standard) applied to
    real token counts - the nightly log's own cost-telemetry line, and
    poll_and_ingest_batch()'s own summary."""
    standard = (input_tokens / 1_000_000) * TOP100_INPUT_USD_PER_MTOK + \
        (output_tokens / 1_000_000) * TOP100_OUTPUT_USD_PER_MTOK
    return standard * BATCH_DISCOUNT


def refresh_all(log=print):
    """Owner-only "refresh all" button (Commit 1's own cadence rule:
    "full re-score quarterly plus an owner-only refresh all button") -
    re-selects the pool, then submits EVERY pooled company (not just
    unscored entrants) for the CURRENT quarter/model.

    SMALL FIX (25 Sep 2026, owner-reported): used to submit under a
    synthetic quarter suffix ("2026Q3-refresh-<date>", a DIFFERENT
    cache key from the plain quarter the rest of the pipeline reads/
    writes) specifically to bypass submit_nightly_batch()'s own
    "already scored, skip" filter. That caused two real bugs -
    confirmed directly, not assumed: (1) top100_render.py's
    _enriched_pool(), "the one place every tab reads from", queries
    the PLAIN quarter key only, so a refresh's results never actually
    appeared on the page at all; (2) any later-same-day submission
    (this module's own hourly-poll auto-resubmit, or that night's
    regular run_nightly()) never saw the refresh-tagged rows as
    "already scored" at the plain key, so it would re-submit and
    re-score the exact same companies the owner just paid for minutes
    earlier. Now passes force=True to submit_nightly_batch() instead -
    bypasses the same filter, but saves under the SAME plain quarter
    key every other caller uses, so the page shows the refreshed
    result as soon as it's ingested and nothing pays twice for the
    same (ticker, quarter, model) the same day. Losing the separate
    cache key loses no failure-safety: save_score()'s own per-ticker
    UPSERT already only ever touches a ticker that actually SUCCEEDED
    in the batch (see submit_nightly_batch()'s own force= docstring) -
    that guarantee never came from the suffix. Returns the submitted
    batch id, or None.

    ASX extension (Top 20 Australia guaranteed-twenty, 25 Sep 2026):
    re-selecting also re-selects the extension (select_top100_pool()'s
    own job), so this explicitly folds top100_store.current_asx_
    extension() into the submitted pool too - "Refresh all" refreshes
    Australia's guaranteed twenty exactly as thoroughly as the global
    100, not just the tickers that happen to already be in the 100."""
    pool = select_top100_pool(log=log)
    extension = top100_store.current_asx_extension()
    return submit_nightly_batch(pool=pool + extension, quarter=current_quarter(), model=MODEL_TOP100, log=log, force=True)


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
    possible reading on a dimension)."""
    if score_row is None or score_row.get("not_rated"):
        return None

    available_weight = 0.0
    ai_component = 0.0
    for key in DIMENSION_KEYS:
        dim = score_row["dims"].get(key) or {}
        score = dim.get("score")
        if score is None:
            continue
        weight = DIMENSION_WEIGHT[key]
        available_weight += weight
        ai_component += weight * (score - 1) / 4.0
    if available_weight <= 0:
        return None

    return round(ai_component * (100.0 / available_weight), 2)
