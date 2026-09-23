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
import re
from datetime import datetime, timezone

import scan_store
import top100_store

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
    store.save_pool(as_of=today's UTC date). Returns the saved pool -
    [{"ticker","company_name","universe","value_score","mos_pct",
    "price","intrinsic_value","currency"}, ...], Value Score
    descending. Never raises - a single bad universe file is skipped
    (scan_store.load_scan_raw() itself already returns None on any
    read error), and an empty result (no saved scans yet) simply
    persists/returns an empty pool rather than crashing."""
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
                }

    pool = sorted(best_by_ticker.values(), key=lambda r: r["value_score"], reverse=True)[:POOL_SIZE]
    as_of = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    top100_store.save_pool(pool, as_of)
    log(f"[top100] selected {len(pool)} companies for {as_of} "
        f"(from {len(best_by_ticker)} deduped candidates across "
        f"{len([u for u in scan_store.list_saved_universes() if u != IMPORTED_UNIVERSE])} universes)")
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

# (key, label, weight) - the nine AI-SCORED dimensions. The tenth
# composite component, "return", is Python-computed (see composite_
# score() below) and carries WEIGHT_RETURN, not a model-scored
# dimension - kept in this same block purely so every weight in the
# composite lives in ONE place, per the task's own "all weights in one
# constants block for easy tuning" instruction.
WEIGHT_RETURN = 18
DIMENSIONS = [
    ("ai_exposure", "AI exposure", 13),
    ("regulatory", "Regulatory", 10),
    ("moat", "Moat", 10),
    ("customer_concentration", "Customer concentration", 10),
    ("accounting_quality", "Accounting quality", 8),
    ("balance_sheet_resilience", "Balance-sheet resilience", 8),
    ("pricing_power", "Pricing power", 8),
    ("capital_allocation", "Capital allocation", 6),
    ("reinvestment_runway", "Reinvestment runway", 5),
    ("inflation_exposure", "Inflation exposure", 4),
]
assert WEIGHT_RETURN + sum(w for _k, _l, w in DIMENSIONS) == 100

DIMENSION_KEYS = [k for k, _l, _w in DIMENSIONS]
DIMENSION_WEIGHT = {k: w for k, _l, w in DIMENSIONS}
DIMENSION_LABEL = {k: l for k, l, _w in DIMENSIONS}

# >= this many null dimensions (out of ten) -> NOT RATED. The task's
# own rule, named here rather than an inline "3" so it reads the same
# in the prompt text, the scoring parser, and the page.
NOT_RATED_MIN_NULLS = 3

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

_SYSTEM_PROMPT = """You are screening publicly-listed companies for a factual, descriptive "Top 100" quality shortlist on an investing research site. You are given one company's ticker and name. Score it on TEN qualitative dimensions, each as an integer from 1 to 5 - 5 is ALWAYS the good outcome for a long-term holder of the stock, 1 is ALWAYS the bad outcome, on every dimension, no exceptions.

For each dimension also give a ONE-LINE justification naming the specific source period it is based on (e.g. "FY25 annual report", "Q2 2026 investor call", "the company's own FY24 10-K risk factors section").

HONESTY RULE, the single most important instruction in this prompt: output a null score (not a number, and never a middle value like 3 to "play it safe") for ANY dimension you do not have confident, specific, public-record knowledge of for THIS company. Guessing a plausible-sounding score is worse than admitting you don't know - a null is the honest answer, a fabricated 3 is not. If three or more of your ten scores end up null, that is expected and correct for a company with a thin public record - do not distort your other scores to avoid it.

THE TEN DIMENSIONS AND THEIR ANCHORS (1 = worst for a holder, 5 = best for a holder):

1. MOAT - durable competitive advantage.
   5: a wide, structurally durable moat (network effects, high switching costs, a regulatory licence, genuine pricing power from brand or scale) with clear multi-year evidence it has persisted.
   1: no discernible moat - a commodity business competing purely on price.

2. PRICING POWER.
   5: has repeatedly raised prices at or above inflation without losing meaningful volume.
   1: a price-taker in a commoditised market; margins are compressed by competition, not defended by pricing.

3. INFLATION EXPOSURE.
   5: revenue and margins are naturally inflation-linked or hedged (e.g. CPI-escalated contracts, real-asset backing, ability to pass costs through immediately).
   1: fixed-price, long-duration contracts or high fixed input costs with no pass-through mechanism - margins erode directly as inflation rises.

4. AI EXPOSURE.
   5: immune to AI disruption, or a clear beneficiary - AI increases demand for what it sells, or the company's own use of AI is a structural cost or product advantage.
   1: the core product or service is directly substitutable by an AI tool at a fraction of the cost.
   Calibration: "INTU regulatory 2 - IRS Direct File: a live, political, existential threat to TurboTax consumer" (this example is about REGULATORY, listed under dimension 5 below, not AI - included here only to show HOW SPECIFIC and HOW HONEST a low score's justification should be).

5. REGULATORY.
   5: regulation is a tailwind that compels purchase of the company's product, or forms a protective moat around it.
   1: an existential political or procurement risk - a plausible single regulatory or policy change could eliminate a large share of revenue.
   Calibration: "PAYX regulatory 4 - payroll complexity only ever increases; each new rule adds compliance demand." "INTU regulatory 2 - IRS Direct File: a live, political, existential threat to TurboTax consumer."

6. CUSTOMER CONCENTRATION.
   5: thousands of small, individually-replaceable customers; no single customer is material to revenue.
   1: one customer is more than 30% of revenue, or a small handful of customers collectively dominate it.
   Calibration: "OCL.AX customer concentration 1 - effectively all revenue is government procurement; the Defence loss was this risk."

7. ACCOUNTING QUALITY (capitalisation rate + cash conversion).
   5: free cash flow conversion of roughly 80-120% of net income, with minimal capitalisation of what are effectively normal operating costs (e.g. R&D, software development) onto the balance sheet.
   1: aggressive capitalisation of operating-like costs materially inflates reported free cash flow; cash conversion sits far below net income.
   Calibration: "OCL.AX accounting 2 - capitalises 52% of R&D, which inflated screener FCF by 56%."

8. CAPITAL ALLOCATION (explicitly including stock-based compensation dilution vs buybacks).
   5: a disciplined reinvestment/M&A track record; buybacks genuinely reduce the share count net of SBC dilution; no pattern of value-destroying write-downs.
   1: a pattern of value-destroying acquisitions or repeated impairments, or buybacks that merely offset SBC dilution without shrinking the real share count.
   Calibration: "SEK.AX capital allocation 2 - repeated impairments on Zhaobin, OCC and Brasil Online aren't bad luck; they're the record."

9. REINVESTMENT RUNWAY.
   5: a long runway of high-return reinvestment opportunities still ahead (an expanding or under-penetrated market) at returns well above the cost of capital.
   1: a mature, saturated market with few remaining high-return reinvestment options - excess cash is likely to be misallocated, or simply returned because there is nowhere better to put it.

10. BALANCE-SHEET RESILIENCE (net debt/EBITDA, interest coverage, maturity wall).
    5: low net debt/EBITDA, high interest coverage, no concentrated near-term refinancing wall.
    1: high leverage, thin interest coverage, and/or a concentrated debt maturity wall in the next 1-2 years creating real refinancing risk.

Also write ONE short "summary" - the company's single strongest dimension and the ONE thing a reader should verify for themselves before relying on this screen. Frame it as homework, never as a recommendation to buy or hold - this is a description of what the data and your knowledge show, not investment advice."""


def _user_prompt(ticker, company_name):
    name = company_name or ticker
    return (
        f"Company: {name} (ticker: {ticker})\n\n"
        "Score this company on all ten dimensions per your instructions, "
        "then write the one-line summary."
    )


def _dimension_schema():
    return {
        "type": "object",
        "properties": {
            "score": {"type": ["integer", "null"], "minimum": 1, "maximum": 5},
            "justification": {"type": "string"},
            "source_period": {"type": ["string", "null"]},
        },
        "required": ["score", "justification", "source_period"],
        "additionalProperties": False,
    }


def _response_schema():
    props = {key: _dimension_schema() for key in DIMENSION_KEYS}
    props["summary"] = {"type": "string"}
    return {
        "type": "object",
        "properties": props,
        "required": DIMENSION_KEYS + ["summary"],
        "additionalProperties": False,
    }


def _request_params(ticker, company_name):
    """The exact MessageCreateParamsNonStreaming-shaped dict for one
    company - shared by submit_nightly_batch() (wrapped in a Batches
    Request) and run_single_test_call() (sent directly, for the
    task's own one real API test call)."""
    return {
        "model": MODEL_TOP100,
        "max_tokens": 2000,
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
    (dims_dict, not_rated, summary). `dims_dict`: {key: {"score",
    "justification","source_period"}, ...} for all ten keys. Raises
    ValueError on malformed JSON or a missing dimension - the caller
    (poll_and_ingest_batch) treats that exactly like any other per-
    ticker failure: logged, skipped, prior cache untouched."""
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
        if score is None:
            null_count += 1
        dims[key] = {
            "score": score,
            "justification": d.get("justification") or "",
            "source_period": d.get("source_period"),
        }
    not_rated = null_count >= NOT_RATED_MIN_NULLS
    summary = data.get("summary") or ""
    return dims, not_rated, summary


# -----------------------------------------------------------------
# Batch submit / poll (two-phase, non-blocking).
# -----------------------------------------------------------------

def _unscored_tickers(pool, quarter, model):
    already = top100_store.scores_for_quarter_model(quarter, model)
    return [r for r in pool if r["ticker"] not in already]


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
    try:
        for result in client.messages.batches.results(state["batch_id"]):
            ticker = custom_id_map.get(result.custom_id)
            if not ticker:
                continue
            if result.result.type != "succeeded":
                failed += 1
                log(f"[top100] {ticker}: batch result {result.result.type}, skipped")
                continue
            msg = result.result.message
            text = next((b.text for b in msg.content if b.type == "text"), "")
            prompt_params = _request_params(ticker, ticker)
            try:
                dims, not_rated, summary = _parse_response_json(text)
            except Exception as e:
                failed += 1
                log(f"[top100] {ticker}: could not parse batch result, skipped ({e})")
                continue
            top100_store.save_score(
                ticker=ticker, quarter=state["quarter"], model=state["model"],
                dims=dims, not_rated=not_rated, summary=summary,
                prompt=json.dumps(prompt_params), raw_response=text,
            )
            saved += 1
            total_input_tokens += getattr(msg.usage, "input_tokens", 0) or 0
            total_output_tokens += getattr(msg.usage, "output_tokens", 0) or 0
    except Exception as e:
        log(f"[top100] batch result retrieval failed partway through: {e}")

    top100_store.clear_batch_state()
    cost = estimate_batch_cost_usd(total_input_tokens, total_output_tokens)
    log(f"[top100] batch {state['batch_id']} ingested: {saved} scored, {failed} failed - "
        f"{total_input_tokens:,} input + {total_output_tokens:,} output tokens, "
        f"est. ${cost:.4f} (batch-priced)")
    return {"saved": saved, "failed": failed,
            "input_tokens": total_input_tokens, "output_tokens": total_output_tokens,
            "cost_usd": cost}


def submit_nightly_batch(pool=None, quarter=None, model=MODEL_TOP100, log=print):
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
    the submitted batch's id."""
    pool = top100_store.current_pool() if pool is None else pool
    quarter = quarter or current_quarter()
    if not pool:
        return None
    entrants = _unscored_tickers(pool, quarter, model)[:MAX_NIGHTLY_SCORES]
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
    {"ticker","dims","not_rated","summary","input_tokens",
    "output_tokens","cost_usd"} - standard, non-batch pricing (this
    call does not go through the Batches API), reported honestly as
    such."""
    import anthropic
    client = anthropic.Anthropic()
    params = _request_params(ticker, company_name)
    resp = client.messages.create(**params)
    text = next((b.text for b in resp.content if b.type == "text"), "")
    dims, not_rated, summary = _parse_response_json(text)
    input_tokens = getattr(resp.usage, "input_tokens", 0) or 0
    output_tokens = getattr(resp.usage, "output_tokens", 0) or 0
    cost = (input_tokens / 1_000_000) * TOP100_INPUT_USD_PER_MTOK + \
        (output_tokens / 1_000_000) * TOP100_OUTPUT_USD_PER_MTOK
    quarter = current_quarter()
    top100_store.save_score(
        ticker=ticker, quarter=quarter, model=MODEL_TOP100,
        dims=dims, not_rated=not_rated, summary=summary,
        prompt=json.dumps(params), raw_response=text,
    )
    return {
        "ticker": ticker, "dims": dims, "not_rated": not_rated, "summary": summary,
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
    unscored entrants) for the CURRENT quarter/model by first clearing
    that quarter's cached scores... except clearing scores would
    violate "any failure leaves prior scores intact" if the refresh
    batch itself then fails. Instead: bumps the effective cache key by
    scoring under a synthetic quarter suffix ("2026Q3-refresh-<date>")
    so every company is genuinely "unscored" for THAT key and gets
    re-submitted, while the real quarter's last-good scores stay
    exactly as they were until the refresh batch actually succeeds and
    is ingested - at which point the page (top100_ui reading "latest
    scored quarter key per ticker") naturally prefers the newer
    result. Returns the submitted batch id, or None."""
    pool = select_top100_pool(log=log)
    refresh_quarter = f"{current_quarter()}-refresh-{datetime.now(timezone.utc).strftime('%Y%m%d')}"
    return submit_nightly_batch(pool=pool, quarter=refresh_quarter, model=MODEL_TOP100, log=log)


# -----------------------------------------------------------------
# Composite (Python-computed, never by the model).
# -----------------------------------------------------------------

def _normalize_mos(mos_pct, pool_mos_values):
    """MOS% linearly rescaled to 0..1 across the CURRENT pool's own
    min/max ("normalised across the 100" - the task's own words) -
    never the raw MOS% itself, which has no natural 0..1 range (it can
    be negative for an overvalued name, or well above 100% for a
    deeply undervalued one). A pool with only one distinct MOS value
    (degenerate - e.g. a tiny test fixture) maps everyone to the
    midpoint (0.5) rather than dividing by zero."""
    if mos_pct is None or not pool_mos_values:
        return None
    lo, hi = min(pool_mos_values), max(pool_mos_values)
    if hi == lo:
        return 0.5
    return max(0.0, min(1.0, (mos_pct - lo) / (hi - lo)))


def composite_score(score_row, mos_pct, pool_mos_values):
    """The 0-100 composite for one company - None for a NOT RATED
    company (score_row is None, or score_row["not_rated"] is True) or
    one with no MOS% at all (mos_pct is None - can't price its own
    "return" component, 18% of the total weight).

    composite = WEIGHT_RETURN * normalized_MOS
              + sum over each NON-NULL AI dimension of
                weight_i * (score_i - 1) / 4
                RESCALED so the AI-dimension portion still sums to
                (100 - WEIGHT_RETURN) even when 1-2 dimensions are
                null (a NOT-RATED company, >= NOT_RATED_MIN_NULLS
                null, returns None entirely rather than reaching this
                rescale at all - see above).

    (score - 1) / 4 maps the 1-5 scale onto 0..1 linearly (1 -> 0.0,
    5 -> 1.0) - a true fraction-of-the-dimension's-own-weight, not the
    score/5 a first glance might suggest (which would map a 1 to 20%
    of the weight rather than 0%, silently rewarding the worst
    possible reading on a dimension)."""
    if score_row is None or score_row.get("not_rated"):
        return None
    mos_norm = _normalize_mos(mos_pct, pool_mos_values)
    if mos_norm is None:
        return None

    ai_weight_total = 100 - WEIGHT_RETURN
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
    scaled_ai_component = ai_component * (ai_weight_total / available_weight)

    return round(WEIGHT_RETURN * mos_norm + scaled_ai_component, 2)
