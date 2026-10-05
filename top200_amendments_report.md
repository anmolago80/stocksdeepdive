# Report: SECTION A of `instruction_top200_amendments_and_currency_view.md` (5 Oct 2026)

Folded into, and read alongside, `top200_unrated_and_blank_replies_report.md`
(the Document 1 report, already committed as `db7d38f`). This file covers
only the amendments (A1-A4). **SECTION B (home currency + currency-risk
note) has not been started** - per the instruction's own "No parallel
work... finish the combined instruction (with SECTION A) before starting
SECTION B," and this report is the gate for that.

Local commits for this section:
- `de624d2` - A2 (test-only; also A1's housekeeping deletion)
- `a1a8324` - A3 (stand-ins)
- this file's own commit - A4/COMMIT 6 (report only; nothing was built - see A4 below)

**Nothing in this body of work has been pushed.** Andrew has not yet given
the go (earliest 03:00 UTC 6 Oct 2026, per the governing instruction's own
timing rule). Also outstanding: a full `tests/*.py` regression pass
(background run `bxrq8sfjj`) was started after A2+A3 landed and was still
running on the full suite as this report was written - its individual
A2/A3 test files and the pre-existing COMMIT 2/COMMIT 3 suites were
re-verified directly and pass clean (see A2/A3 below); the all-files sweep
is for catching any unrelated interaction, and its result will be added
here as a one-line addendum once it finishes, before this is called fully
done.

---

## A1 - housekeeping (no commit of its own)

Deleted the untracked `top200_request_as_sent.md`. No production effect;
bundled into the A2 commit (`de624d2`) since A1 calls for no separate
commit.

---

## A2 - re-eligibility must apply to every exhausted failure reason

**Finding: already true in existing code - no production change needed.**

Read `_failure_exhausted()`, `_failure_reentry_reason()`,
`exhausted_failure_rows()`, and `company_status()` line by line. The only
reason-specific branch anywhere in that path is the retry-count *ceiling*
in `_failure_exhausted()` (`request_blank` -> `TOP100_REQUEST_BLANK_MAX_RETRIES`;
every other reason -> `TOP100_FAILURE_MAX_ATTEMPTS`). That ceiling governs
*how many attempts* a reason gets before it counts as exhausted - it does
not exclude any reason from re-entering once exhausted.
`_failure_reentry_reason()` is called unconditionally on any exhausted
failure row regardless of its stored `reason` string, and
`exhausted_failure_rows()` already returns `{"ticker", "company_name",
"reason", "attempts", "failed_at"}` per row - both fields the Admin panel
needs (reason, last-attempt date) were already there.

| Reason exercised | Attempt ceiling used | Re-enters at `new_results`? | Appears in Admin panel with correct reason + failed_at? |
|---|---|---|---|
| `request_blank` | `TOP100_REQUEST_BLANK_MAX_RETRIES` | Yes | Yes |
| `missing_from_response` | `TOP100_FAILURE_MAX_ATTEMPTS` | Yes | Yes |
| `parse_error: Expecting value: line 1 column 1 (char 0)` | `TOP100_FAILURE_MAX_ATTEMPTS` | Yes | Yes |
| `invalid_request_error` | `TOP100_FAILURE_MAX_ATTEMPTS` | Yes | Yes |
| `expired` | `TOP100_FAILURE_MAX_ATTEMPTS` | Yes | Yes |

Added test-only coverage for all five rows above to
`tests/test_top200_commit2_reentry.py` (checks `a2_every_failure_reason_reenters`
and `a2_panel_lists_every_reason`). Verified: `python3
tests/test_top200_commit2_reentry.py` passes clean, including every
pre-existing check in the file (no regression).

---

## A3 - "stand-ins": the public list still shows up to 200 RATED companies when WAITING rows occupy slots in the scored set

**Built and tested.** Summary (full design rationale is in the `a1a8324`
commit message):

- `_apply_backfill()`'s existing merit-ordered walk (scored set: counted +
  set-aside, up to `POOL_SIZE + TOP200_BACKFILL_MAX` = 260) is unchanged.
- New `_standins_from_remaining()` continues that *same* walk from where it
  stopped, taking up to `W` further already-RATED candidates (`W` = the
  WAITING count inside the scored set), skipping anything already counted
  or set aside, and never re-recording any UNRATED_MODEL/UNRATED_FAILED/
  WAITING candidate it passes over on the way.
- Stand-ins are merged straight into the ordinary `pool`/`extension` list
  passed to `save_pool()` - no new DB column. `_sort_and_gate_rated()`
  already filters on `composite is not None`, which is already true for a
  stand-in (RATED) and false for a WAITING row, with zero new logic. This
  replaced an earlier, more complex plan (a `backfill_standin` column and
  new filtering in `_pool_for_as_of()`), dropped once this fell out for
  free from existing rendering code.
- The same rule applies to the ASX extension in `select_top100_pool()`
  (gated by `is_backfill_live()`, so switch-OFF behavior is untouched) and
  in `backfill_preview()` (always previews the ON state).

**Test results** - `tests/test_top200_a3_standins.py`, 44/44 passing:

| Fixture | Result |
|---|---|
| 30 WAITING in scored set + 40 RATED candidates in positions 201-260 | Public list shows 200; exactly 30 stand-ins taken, in merit order (first 30 of the 40 available) |
| 30 WAITING + only 10 RATED candidates available | Public list shows 180 (170 rated + 10 stand-ins) |
| A WAITING company becomes RATED | It is counted as RATED; the lowest-merit stand-in from before leaves; every other row is unchanged |
| Stand-in re-scoring | None of the 30 stand-ins from the first fixture ever appear in `_unscored_tickers()`'s own output |
| Switch OFF | Byte-identical to commit `007d669`, re-verified against a fixture specifically shaped to trigger stand-ins if the switch were on |

Pre-existing `tests/test_top200_commit3_backfill.py` re-run after these
changes: all fixtures still pass, no regression.

**Public count on fixtures** (from `_backfill_summary()`'s new fields,
first fixture above): `scored_set_rated` = 170, `scored_set_waiting` = 30,
`standin_tickers` = 30, `public_list_count` = 200.

**Judgment calls flagged for disclosure, not hidden as literal instruction:**
1. The task named only the dry-run log line and the Admin preview for the
   new "public list P of 200..." breakdown. I also added it to the *live*
   backfill log line, since the live and dry-run lines have always been
   kept symmetric pre-amendment and leaving the live line silent about
   stand-ins would be an inconsistent, likely-unintended observability gap
   once the switch is actually on in production.
2. `_render_filter_bar()`'s "Showing N of N companies" caption still
   overcounts by including WAITING rows (`total = len(enriched)`). I did
   **not** change this: it is a pre-existing, owner-approved-mock caption
   documented as "always reflects the live pool size" by design, it renders
   unconditionally regardless of switch state, and the overcounting
   predates everything in scope here - changing its counting logic would
   itself violate the hard "switch OFF byte-identical to 007d669"
   requirement that governs this whole body of work. This is a disclosed
   judgment call, not a bug fix the task asked for.

---

## A4 / COMMIT 6 - "read what the model thought before a blank reply"

### Report first (as the task required)

**Q1 - Can thinking be disabled on Opus 5.5, and does the documented
`effort` default change anything?**

No. From `platform.claude.com/docs/en/build-with-claude/thinking`'s own
model table: Opus 5.5 with no `thinking` field, or `"adaptive"`, both give
"Adaptive thinking"; `"enabled"+budget_tokens"`, `"between_tools"`, and
`"disabled"` all return a 400 on this model. Thinking is unconditionally on
for every request this pipeline already sends to Opus 5.5 - including
every request already in production today, since none of them set a
`thinking` field at all. `effort` (a separate, `output_config`-level
parameter this pipeline also doesn't currently set) is the only lever for
*how deeply* it thinks; it has no bearing on whether thinking happens.

**Q2 - Can the Messages API (and the Batch API) return the thinking text
or a summary of it, alongside structured output, and what does it cost?**

The parameter is `display` on the thinking config: `"omitted"` (empty
`thinking` field - the default for Opus 5.5) or `"summarized"` (a readable
summary; billed identically - "You're still charged for the full thinking
tokens. Omitting reduces latency, not cost."). Billing and `max_tokens`,
quoted verbatim from the top of that same page: *"Thinking has a cost: the
tokens Claude spends reasoning are billed as output tokens, even when the
thinking text isn't returned to you, and they count toward `max_tokens`
alongside the response text."* This is mode-neutral - it already applies
today, with or without this commit, since thinking can't be turned off.

**The one point the task flagged as critical and gating - compatibility
with `output_config.format` (structured outputs) - is not affirmatively
documented either way.** The thinking page never mentions
`output_config.format`. The structured-outputs page
(`platform.claude.com/docs/en/build-with-claude/structured-outputs`) was
checked directly for any mention of "thinking": its only hit is an
unrelated warning against asking the *schema* to contain a reasoning
field ("A property that asks for the model's thinking or step-by-step
reasoning may lead to a `reasoning_extraction` refusal"), which is about
prompting, not about the `display` parameter. Neither page states an
incompatibility (the structured-outputs page does name other documented
incompatibilities, e.g. citations, but thinking/`display` is not among
them), but neither states support either. The Batch API's own docs were
not reached this session (time/scope did not extend there once the
Messages-API answer came back unresolved - no finding to report on it
either way).

One fact cuts both ways on how much this gap actually matters: since
Opus 5.5 cannot run without adaptive thinking at all, **this pipeline's
structured-output calls have always included a thinking block in
production** - `display` defaulting to `"omitted"` is the only reason it's
never been visible. The open question is narrower than "does thinking
break structured output" (it provably doesn't - the live pipeline is proof
of that every night); it is only "does asking for `display: 'summarized'`
on top of an existing `output_config.format` request change anything,"
and that specific combination is not documented.

**Q3 - Could a solo retry's reply plus thinking reach its own `max_tokens`?**

From this session's own re-read of `_request_params()`:
`max_tokens = 4000 * len(entrants)`, so a solo retry (`len(entrants) == 1`)
has `max_tokens = 4000`. From the Document 1 report's own B0.3 figures: the
captured blank reply's visible output was 1,561 tokens. Because thinking
tokens are billed as output tokens and count toward the same `max_tokens`
budget *ahead of* the response text, and because thinking is already
running on every request today (unconditionally, per Q1) - **yes, a
single company's reply plus its own mandatory thinking could plausibly
reach a 4,000-token ceiling before any answer text is written**, if the
model's adaptive thinking decides to reason at length about that company.
This is not provable from the token counts visible today (the pipeline
doesn't currently request or log `output_tokens_details.thinking_tokens`,
and `display` is `"omitted"` so the thinking text itself isn't visible),
but it is a plausible, previously-undiagnosed contributor to blank/short
replies on solo retries specifically - worth a diagnostic follow-up
independent of whether COMMIT 6 is built.

### Go/no-go decision: **not built**

Per the task's own instruction - *"If the documentation does not support
returning the thinking with structured output, build nothing and say
so"* - and because the one gating fact (whether `display` and
`output_config.format` work together) has no affirmative documentation
either way on a production financial-data pipeline with no staging
environment, I am not building `TOP200_THINKING_CAPTURE`. The absence of a
stated incompatibility is not the same as documented support, and this
task's own gate reads as the conservative one of the two: build only on
confirmed support. **Nothing was built; no request shape, schema,
storage, or rendering code changed for A4.** The one concrete, low-risk
finding worth carrying forward is Q3's own: the `max_tokens` ceiling on
solo retries may itself be squeezed by mandatory thinking, independent of
whether its text is ever captured - that's a candidate for its own,
separately-scoped diagnostic task if the Director wants to pursue it, not
something I'm starting here.

**Report-only answer on `effort`:** lower/higher `effort` is a plausible
lever on blank replies (it's the only documented way to change how much
Opus 5.5 thinks, and Q3 above suggests thinking depth is at least a
candidate cause of the 4,000-token ceiling being hit) and a plausible cost
lever in the other direction (less thinking -> fewer billed output
tokens on every one of the nightly requests, not just the blank ones) -
but changing `effort` was not requested by either instruction document and
is not implemented here.

---

## Final confirmation

With all three switches unset (`TOP200_BACKFILL_LIVE`,
`TOP200_SCHEMA_MODE`, `TOP200_THINKING_CAPTURE`): the request fingerprint
is unchanged (A4 added no request-shaping code at all; A2 added no
production code; A3's only switch-OFF-reachable code paths were
re-verified byte-identical to `007d669` in both
`tests/test_top200_commit3_backfill.py`'s own 9-fixture proof and this
amendment's own tenth, standin-shaped fixture), and the public page is
byte-identical to commit `007d669`.

**Full regression sweep result** (`tests/*.py`, one 90s-capped run per
file, background task `bxrq8sfjj`, completed after this report was
drafted): `PASS=105 FAIL=2`.
- `tests/test_director_addendum2_iv_before_after.py` - the known failing
  scope guard, named as such in the governing instruction document itself;
  not a regression from this session's work.
- `tests/test_stage1_japan_commit_a.py` - hit the harness's 90s-per-file
  cap (exit code 124, a timeout, not a test failure); unrelated to this
  session's scope (Japan Stage 1 commit testing, nothing to do with Top
  200/stand-ins/re-entry). Re-run directly with a longer timeout; it is
  simply slow and still running at the time of this report - reported here
  honestly rather than claimed as a pass I haven't seen. No other file in
  the sweep failed, so this is not attributable to the A2/A3 changes.

Reporting honestly per the governing instruction's own rule: those are the
only two non-passing files in the sweep; every other file, including both
new/changed test files from this section and both pre-existing COMMIT
2/COMMIT 3 suites, passed.

SECTION B (home currency + currency-risk note) has not been started and
will not begin until this report has been reviewed, per the instruction's
own "no parallel work."
