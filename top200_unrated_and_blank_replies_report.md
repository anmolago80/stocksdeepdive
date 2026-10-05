# Report: Top 200 - unrated companies, "Not rated" section, automatic coverage count, blank-reply fix, and the "rating on what is known" dry run

For the Director, per `instruction_top200_unrated_and_blank_replies_combined.md` (5 Oct 2026). All five commits are **built, tested, and committed locally** on `main`. **Nothing has been pushed** - Andrew has not yet given the go (earliest 03:00 UTC 6 Oct, after reading the 5 Oct 23:00 UTC run, per the instruction's own timing rule).

Local commits:
- `8b1acb8` - COMMITS 1-4 (bundled; see Section 3's own note on why)
- `fbd9688` - COMMIT 5

---

## 1. STEP A0 - inventory (read-only)

1. **Today's presentation of each state (pre-COMMIT-3 code, commit `007d669`):** a RATED company renders normally. A NOT RATED company (model's own judgement, or `degenerate_accepted`) is shown in the Full 100/200 tab's **bottom shelf** (`top100_render._render_bottom_shelf()`), sorted by Value Score among themselves, reusing caption keys `shelf_caption_not_rated`/`shelf_caption_scoring_failed`/`shelf_caption_degenerate_x2` - never distinguished from a plain scoring failure in the shelf UI itself (same shelf, same visual treatment). A company failed and awaiting retry, or a deferred newcomer, or one in a pending batch, is simply **absent** - it holds a pool slot but shows nothing distinct; it's indistinguishable from a RATED company that just hasn't rendered yet, from the page alone. The Top 20 tabs exclude every NOT RATED company entirely (`_sort_and_gate_rated()` only returns rows with `composite is not None`) - no shelf there at all.
2. **Persistence rule:** `_true_newcomer_gated()` / `pool_presence` table, gated by `consecutive_nights < 3`; the constant is 3 nights. A deferred newcomer **does** count as one of the 200 (it holds its pool slot; selection doesn't re-derive the 200 around it) - it's simply excluded from `submit_nightly_batch()`'s own batch that night, not from the pool.
3. **Merit order:** `select_top100_pool()`'s `best_by_ticker` values sorted by `value_score` (descending) - this is `_build_best_by_ticker()`'s own output, consumed as `merit_ordered` in COMMIT 3. **Public rank order:** `top100_render._sort_and_gate_rated()`'s `_key_research` (Research Score, i.e. `composite_score()`) under the default sort; three other sort keys exist (`_key_value_today`, `_key_value_score`, `_key_severity`) selectable via the sort bar.
4. **Failure reasons stored in `top100_score_failures.reason`** (from `record_score_failure()` call sites in `poll_and_ingest_batch()`): `request_blank`, `degenerate_response`, `missing_from_response`, `parse_error: <detail>`, and the batch-result error type string for an `errored` result (e.g. `invalid_request_error`) or `result.result.type` for a non-`succeeded`/non-`errored` status. Retry rule: `_failure_exhausted()` - `request_blank` is capped at `TOP100_REQUEST_BLANK_MAX_RETRIES`; every other reason is capped at `TOP100_FAILURE_MAX_ATTEMPTS`, gated additionally by `TOP100_FAILURE_RETRY_HOURS` (`_failure_blocks()`). End state: once exhausted, `company_status()` classifies the row `UNRATED_FAILED`; COMMIT 2 is what gives it a way back (re-entry on the same new-results/age triggers NOT RATED rows already get).
5. **Top 20 tabs:** `tab_mixed`/`tab_us` = `_sort_and_gate_rated(enriched, sort_mode)[:20]` (global pool, optionally currency-filtered); `tab_au` = `_sort_and_gate_rated(pool_au + extension, sort_mode)` with NO explicit slice - its own "guaranteed 40" comes from the ASX extension's own candidate-count target (`TOP20_AU_TARGET = 40`), computed in `select_top100_pool()`'s own extension block (`needed = TOP20_AU_TARGET - au_in_pool`, filled from the next-best `.AX` candidates by Value Score).
6. **Model-judged NOT RATED vs. `degenerate_accepted`:** both are `not_rated = True` on the stored row; the model's own judgement has `degenerate_accepted = False`, the two-strike acceptance path sets `degenerate_accepted = True` (via `_save_degenerate_as_not_rated()`). `company_status()` reads exactly this to classify `UNRATED_MODEL` vs `UNRATED_FAILED`.
7. **Can a `request_blank` company be saved as NOT RATED?** No - confirmed by re-reading the code this session (not just re-stated from the earlier, corrected A1 report): in `poll_and_ingest_batch()`, a ticker in `_request_blank_tickers` hits its own `if ticker in _request_blank_tickers:` branch first (`failed += 1; failure_reasons.append((ticker, "request_blank", ...)); continue`) - it **never** reaches the `_degenerate_tickers`/`_save_degenerate_as_not_rated()` branch below it, and never reaches the ordinary `top100_store.save_score()` call either. A whole-blank request is recorded as a pure failure for every one of its entrants, never a NOT RATED save, by construction of the branch order.
8. **Re-score triggers on NOT RATED rows:** the same `_unscored_tickers()` reasons as any row - `new_or_rubric` (no current-rubric row at all), `new_results` (a newer `most_recent_quarter` than the stored row), `age` (`RESCORE_MAX_AGE_DAYS` elapsed). They depend on the company **still being in the pool** - `_unscored_tickers(pool, model)` only ever iterates `pool`'s own rows; a NOT RATED company that has left the pool entirely is never reconsidered by this path (though COMMIT 3's backfill, when live, keeps a set-aside company's STATUS tracked independently of whether it's still "in the pool" in the legacy sense).

**S1/S2/S3, confirmed:**
- **S1 (one merit-ordered list, first 200):** confirmed - `_build_best_by_ticker()` + `select_top100_pool()`'s own `sorted(best_by_ticker.values(), key=value_score, reverse=True)[:POOL_SIZE]` (switch OFF) is exactly this.
- **S2 (four statuses derivable from stored data alone):** confirmed - `company_status(score_row, failure)` needs only `top100_store.latest_scores_for_model()` + `score_failures_for_model()`, no network call.
- **S3 (removing an unrated company doesn't reorder the rated survivors):** confirmed - `_apply_backfill()` walks `merit_ordered` in its own fixed order, appending to `counted` only; nothing re-sorts `counted` afterward. Proven on fixtures (positions 5/60/199 test, COMMIT 3).

---

## 2. STEP B0 (5 answers) + STEP C0 (5 answers, with the ranking-score function)

### STEP B0

1. **`_request_params()` for a packed request** (see `top200_request_as_sent.md`, generated from the live code on 5 Oct): `model` = `claude-opus-5-5`; `max_tokens` = `4000 * len(entrants)` (20000 for a full 5-entrant pack); no `thinking`, `temperature`, or `top_p` parameter is set anywhere in the returned dict; the structured-output parameter is `output_config.format` = `{"type": "json_schema", "schema": <the per-request schema>}`; instructions are sent as `system` (a list, `[{"type": "text", "text": _SYSTEM_PROMPT}]`), the company list as `messages[0]` (`role: "user"`). No other top-level key is present (`model`, `max_tokens`, `system`, `messages`, `output_config` - five keys total).
2. **Schema top-level shape:** an **object** (`{"type": "object", "properties": {"companies": {"type": "array", "items": <per-company schema>}}, "required": ["companies"], "additionalProperties": false}`) - the array itself has no `minItems`/`maxItems` constraint (confirmed: not present anywhere in `_response_schema()`/`_company_item_schema()`).
3. **Sentinel-filled single item token count:** built a fixture item with every dimension score 0, every free-text field `""`, serialized via `json.dumps`, counted with the same `_CHARS_PER_TOKEN_ESTIMATE = 4` heuristic `_estimate_request_tokens()` itself uses (chars/4). A fully-sentinel item's own JSON text is far smaller than a genuine 5-company reply with real justification text - the captured blank reply's 1,561 output tokens is consistent with the model writing the sentinel-shaped STRUCTURE but with each dimension's `justification`/`source_period` strings still populated with boilerplate-length text (the honesty-rule prose is still produced per field even when the score itself is the 0 sentinel) - i.e. the token count reflects ten dimension objects' worth of string scaffolding, not a minimal sentinel.
4. **`_whole_response_is_degenerate()`** checks: every item's `ticker` field is blank/missing AND every dimension score across every item is identical (the sentinel-template signature) - looks at `ticker` + all ten `score` fields per item, nothing else. The **per-item** degenerate check (`_is_degenerate_item()`) looks at: all ten dimension scores identical to each other, AND every justification empty, AND every one of the free-text synthesis fields (`inversion_scenario`, `current_headwind`, `market_structure_comment`, `one_foot_comment`, `munger_comment`, `big_wave_comment`) empty - i.e. the full sentinel-template shape for that one item, not just its scores.
5. **Report only, not implemented - fixed/required slots with `const`/`enum`:** platform.claude.com/docs's own "JSON Schema limitations" section (checked 5 Oct) states structured outputs supports neither `minLength`/`maxLength`/`minimum`/`maximum` nor array-length constraints (`minItems`/`maxItems`) - this is the same limitation this codebase already hit and designed around (see `_dimension_schema()`'s own v3 comment, "zero union types, zero min/max"). It does **not** list `const`/`enum` as unsupported for a STRING property specifically, but this codebase's own schema already avoids every vocabulary-constrained field (`market_structure`, `one_foot_hurdle`, etc.) being declared as an `enum` for exactly this reason (`_company_item_schema()`'s own comment: "structured outputs doesn't support enum validation any more than min/max"). Fixing `ticker` via `const`/`enum` per fixed slot would mean one SEPARATE schema per request (ticker values baked into the schema itself, not sent as data) - a larger, more invasive change than the task's own "only a character count worth of care" framing wants, and not something I have evidence is even supported given the enum caveat already on record. **Not implemented**, per the task's own instruction.

### STEP C0

1. **Ranking-score function:** `top100_engine.composite_score(score_row)` (full text, see `top100_engine.py:4731` after COMMIT 5's refactor):
   ```python
   def composite_score(score_row):
       if score_row is None or score_row.get("not_rated"):
           return None
       return _composite_score_from_dims(score_row["dims"])

   def _composite_score_from_dims(dims):
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
   ```
   **A 0-dimension (null) inside a RATED company (1 or 2 gaps) is LEFT OUT and the remaining weights RESCALED** to fill the full 0-100 scale (`available_weight` only sums the SCORED dimensions' own weights; the final multiplier is `100.0 / available_weight`, not `100.0 / DIMENSION_WEIGHT_TOTAL`). This is exactly the Director's own stated concern for COMMIT 5: a company with 6 good scores and 4 gaps is rescaled to the SAME 100-point ceiling as a company with all 10 scored, so 6 strong dimensions can outrank 10 honest ones with one weak score.
2. **Do stored NOT RATED rows keep the real dimension scores/justifications?** **Yes, confirmed empirically this session** (not just by code reading) - built a synthetic 7-scored/3-null reply, parsed and saved it via the real `save_score()` pipeline, read it back via `latest_scores_for_model()`. Shape of the stored NOT RATED fixture row's `dims`:
   ```json
   {
     "ai_exposure": {"score": 4, "justification": "immune to AI substitution", "source_period": "FY25"},
     "competitive_position": {"score": null, "justification": "", "source_period": null},
     "regulatory_legal": {"score": 3, "justification": "moderate litigation exposure", "source_period": "FY25"},
     "customer_concentration": {"score": null, "justification": "", "source_period": null},
     "pricing_power": {"score": null, "justification": "", "source_period": null},
     "accounting_quality": {"score": 4, "justification": "clean cash conversion", "source_period": "FY25"},
     "balance_sheet_fixed_charges": {"score": 3, "justification": "moderate leverage", "source_period": "FY25"},
     "management": {"score": null, "justification": "", "source_period": null},
     "capital_allocation": {"score": 4, "justification": "disciplined M&A", "source_period": "FY25"},
     "reinvestment_runway": {"score": 3, "justification": "some runway left", "source_period": "FY25"}
   }
   ```
   The 7 real scores + justifications survive untouched; only the 3 null dimensions are nulled. This is the condition the task sets for building COMMIT 5 at all - **confirmed, COMMIT 5 was built.**
3. **Where "three or more zeros" lives:** `NOT_RATED_MIN_NULLS = 3` (`top100_engine.py:1031`). The **only** place it is read: `_parse_one_company()`, `not_rated = null_count >= NOT_RATED_MIN_NULLS` (`top100_engine.py:1836`). No other read site exists in the codebase (grep-confirmed).
4. **Do market_structure/one_foot_hurdle/Munger/big_wave survive a NOT RATED row?** **Yes, confirmed empirically** (same fixture as above) - `_parse_one_company()`'s `if not_rated:` block forces **only** `inversion_scenario`, `inversion_severity`, and `current_headwind` to `None`; `market_structure`/`one_foot_hurdle`/`munger_quality`/`big_wave` (and their comments) are validated **independently**, against their own small vocabulary, with no reference to `not_rated` at all. In the fixture: `market_structure="oligopoly"`, `one_foot_hurdle="no"`, `munger_quality="no"`, `big_wave="tailwind"` - all four, plus their comments, survived on a company that is `not_rated=True`. **How the public page shows an empty inversion/headwind today:** `top100_render._inversion_line_html()`/`_headwind_line_html()` both short-circuit to `""` (nothing rendered at all, not an empty placeholder) whenever `score_row.get("not_rated")` is true - moot for a NOT RATED row specifically on the public page today, since NOT RATED companies never reach the row-rendering path at all (shelved or, under COMMIT 3, listed in the collapsed "Not rated" section instead, which reuses shelf captions rather than rendering per-company inversion/headwind).
5. **Everything else that depends on NOT RATED status:** Top 20 tab eligibility (`_sort_and_gate_rated()` excludes every `not_rated` row from every tab, unconditionally); the Full 100/200 tab's bottom shelf (switch OFF) / "Not rated" section (switch ON); the Admin "Stored score viewer"/"Batch inspector" panels (owner-only diagnostics, not public); **no public API dependency at all** - `api_v1.py` has no Top 100/200 endpoint whatsoever (grep-confirmed: zero occurrences of "top100"/"Top 100" in `api_v1.py`), so there is nothing to check there.

---

## 3. Per-commit summary

A note on commit granularity: **COMMITs 1-3 were already applied to the working tree before this session resumed** (the session was continued after a context compaction, and per the session's own carried-forward summary, COMMITs 1-3 were marked "DONE" with no git commit boundary between them). Rather than risk corrupting already-verified byte-identical behavior with a retroactive, error-prone `git add -p` split across six files and ~1600 lines, COMMITs 1-4 were committed together as `8b1acb8`, with each commit's own scope fully itemized in that commit's message and below. COMMIT 5 (built entirely within this session, with a clean boundary) is its own commit, `fbd9688`.

| Commit | Local hash | What changed |
|---|---|---|
| 1 | `8b1acb8` | `company_status()`/`coverage_for_rows()`/`_log_coverage()`/`zeros_breakdown_for_rows()`/`_log_zeros_breakdown()`/`waiting_detail_for_rows()`/`unrated_failed_detail_for_rows()` in `top100_engine.py`; Admin panel (`_render_top200_coverage_panel()`) in `app.py` |
| 2 | `8b1acb8` | `_failure_reentry_reason()` inside `_unscored_tickers()`; attempts-reset block in `submit_nightly_batch()`; `exhausted_failure_rows()`/`clear_exhausted_failure_rows()`; Admin panel extension |
| 3 | `8b1acb8` | `_build_best_by_ticker()`/`_apply_backfill()`/`_backfill_summary()`/`backfill_preview()`; `top100_store.current_backfill_set_aside()` + `backfill_set_aside` columns; public "Not rated" section; owner preview panel |
| 4 | `8b1acb8` | `top200_schema_mode()`; `schema_mode` threaded through `_company_item_schema()`/`_response_schema()`/`_request_params()`; `submit_nightly_batch()`/`poll_and_ingest_batch()` wiring; `schema_mode_for_batch()`; Batch inspector CSV columns; Admin breakdown table |
| 5 | `fbd9688` | `_composite_score_from_dims()` extraction; `_k_scored()`/`_composite_score_gap_as_middle()`/`partial_rating_dry_run()`/`_log_partial_rating_dry_run()`; Admin panel with 5 tables + CSV |

### Test table (per commit)

| Commit | Test file | Result |
|---|---|---|
| 1 | `tests/test_top200_commit1_coverage.py` | ALL PASSED (7 checks incl. byte-identical-to-007d669, owner-only gate) |
| 2 | `tests/test_top200_commit2_reentry.py` | ALL PASSED (7 checks incl. byte-identical-to-007d669) |
| 3 | `tests/test_top200_commit3_backfill.py` | ALL PASSED (8 checks incl. 9/9 switch-OFF fixtures byte-identical, 0 mismatches) |
| 4 | `tests/test_top200_commit4_schema_mode.py` | PASS=42 FAIL=0 |
| 5 | `tests/test_top200_commit5_partial_rating.py` | PASS=31 FAIL=0 |

### Full regression (run after each commit landed; final run after COMMIT 5)

**PASS=104 FAIL=2.** The two non-passing tests, every time, with no new failure introduced by any of the five commits:
- `tests/test_director_addendum2_iv_before_after.py` - the **known failing scope guard** the instruction itself discloses (never claimed "all tests pass").
- `tests/test_stage1_japan_commit_a.py` - times out (rc=124) attempting a **live** `yfinance`/network fetch; this sandbox has no outbound network access (confirmed by re-running it standalone - it reproduces the same timeout on real `ConnectionError`/`curl: (7) CONNECT tunnel failed` output, nothing related to any change here). Pre-existing, unrelated to this instruction.

### Request fingerprint (with `TOP200_SCHEMA_MODE` unset)

Re-verified after COMMIT 4 and again after COMMIT 5:
```
f00c69f5392af5e2ca435fb4c8a314393c14b4c0dc498cc4293a59d1276e5196
```
Matches the value recorded in the governing instruction document, every time.

---

## 4. COMMIT 2 - how an exhausted company re-enters

A `request_blank`/other-reason company whose attempts are exhausted was, before this work, permanently stuck (no stored score, no further retry). COMMIT 2 makes it eligible again under the **exact same** triggers a NOT RATED row already gets:

- `_unscored_tickers()`'s own `_failure_reentry_reason(ticker, row)` (new, nested inside `_unscored_tickers()`) checks, for a ticker whose failure is exhausted: does the pool row's `most_recent_quarter` differ from what it was at the time of the failure (`new_results`), or has `RESCORE_MAX_AGE_DAYS` elapsed since the failure (`age`)? Either fires exactly like it would for a NOT RATED score row.
- When `_failure_reentry_reason()` returns a reason, `_failure_blocks()` is bypassed for that ticker (it would otherwise unconditionally block an exhausted failure) and the ticker re-enters `_unscored_tickers()`'s own return list under that reason.
- `submit_nightly_batch()`, right after building `entrants`, finds every re-entered ticker that WAS exhausted (`_failure_exhausted()` true before this run) and calls `top100_store.clear_score_failure()` on it - **attempts reset to zero** - logging `[top100] re-entry: N previously exhausted ticker(s) eligible again, attempts reset: <tickers>`. Without this reset, the very next failure would push the ticker straight back past its own exhaustion ceiling, re-exhausting it immediately.
- The Admin panel (`_render_degenerate_accepted_panel()`'s COMMIT 2 extension) lists exhausted-failure rows in the same table as `degenerate_accepted` rows, tagged by kind; "Clear and re-send" clears either kind the same way. A NOT RATED-by-model row is never listed and never cleared by this button (confirmed by `exhausted_failure_rows()`'s own filter).

---

## 5. COMMIT 3 - Top 20 tabs, public strings (EN/ES), switch-OFF mismatch count

**Top 20 tabs:** the unambiguous part - "an unrated company never takes one of the 20 places" - is applied: `select_top100_pool()`'s own extension-candidate generator excludes every set-aside ticker (`and t not in _set_aside_tickers`), so a set-aside `.AX` company can never be independently claimed by the extension (which would otherwise risk two conflicting `save_pool()` upserts for the same row).

What is **left as a mechanical side effect, not a deliberate design decision** (flagged in the code's own comment, re-verified this session by reading the live code rather than re-stating the earlier note verbatim): because the extension's own candidate list is `sorted(... filtered to exclude pool + set-aside ...)[:needed]`, excluding a set-aside AU candidate from consideration means the **next-best** AU candidate is pulled into the extension automatically - the extension effectively backfills itself to the 40 target, the same mechanical sort-and-slice as before this commit, just over a smaller filtered candidate set. Two ways to read this, for Andrew/the Director to pick between:
- **(a) Accept it as correct** - it already mirrors the main 200's own "next in line on merit" principle, applied consistently to the extension too. No further code change needed.
- **(b) Reject it** - if the extension should instead show FEWER than 40 members on a night when a top AU candidate is unrated (never silently substituting a lower-ranked replacement), an explicit cap on `needed` (independent of how many candidates get filtered out) would need to be added - not yet built, since it was never requested.

**Public strings**, `i18n.py` (new keys, EN then ES):
```
top100.not_rated_section_heading:        "Not rated"
                                          "No evaluadas"
top100.not_rated_section_caption:        "These companies ranked high enough on the numbers to be
                                          considered for the Top 200 but have no rating. They are
                                          not part of the ranking above."
                                          "Estas empresas puntuaron lo suficiente en los números
                                          como para ser consideradas para el Top 200, pero no
                                          tienen una calificación. No forman parte de la
                                          clasificación anterior."
top100.not_rated_group_model_heading:    "Not enough information to rate"
                                          "Información insuficiente para calificar"
top100.not_rated_group_model_caption:    "The model judged that it did not know the company well
                                          enough to rate it."
                                          "El modelo determinó que no conocía la empresa lo
                                          bastante bien como para calificarla."
top100.not_rated_group_failed_heading:   "Could not be rated"
                                          "No se pudo calificar"
top100.not_rated_group_failed_caption:   "Rating was attempted and failed. It will be tried again
                                          later."
                                          "Se intentó la calificación y falló. Se volverá a
                                          intentar más adelante."
```

**Switch-OFF mismatch count:** **0 of 9** fixtures - `select_top100_pool()`'s own output is byte-identical to commit `007d669` across all nine, confirmed in the test's own printed line: `[switch_off_byte_identical] 9 of 9 switch-OFF fixtures byte-identical to 007d669 (mismatch count: 0) OK`.

---

## 6. COMMIT 4 unified diff, parser answer / COMMIT 5 tables

### COMMIT 4

Unified diff of the complete request for the task's own 5 fixture entrants (ADP / CSL.AX / AZN_FIXTURE.L / RY_FIXTURE.TO / 7203_FIXTURE.T), legacy vs. `ticker_first`:
```diff
--- 
+++ 
@@ -24,6 +24,10 @@
             "items": {
               "type": "object",
               "properties": {
+                "ticker": {
+                  "type": "string",
+                  "description": "The exact ticker of the company this item answers for, copied verbatim from the list in the user message - used to match this result back to its entrant regardless of response order."
+                },
                 "ai_exposure": {
                   "type": "object",
                   ...
@@ -243,10 +247,6 @@
                     "source_period"
                   ],
                   "additionalProperties": false
-                },
-                "ticker": {
-                  "type": "string",
-                  "description": "The exact ticker of the company this item answers for, copied verbatim from the list in the user message - used to match this result back to its entrant regardless of response order."
                 },
                 "inversion_scenario": {
                   "type": "string",
                   ...
```
Verified programmatically (not eyeballed) that every other per-item property name is **absent** from the diff, and that `model`/`max_tokens`/`system`/`messages`/`required` are all byte-identical between the two modes.

**Did the parser need any change?** **No** - confirmed empirically, not just asserted: the same company item, serialized with `ticker` first vs. last among its own JSON keys, parsed via `_parse_response_json()` to the **identical** result both times. `_parse_one_company()`/`_parse_response_json()` read every field by dict key (`data.get(key)`), which is already order-independent in Python regardless of the source JSON's own property order - no code change was needed, only the schema's declaration order.

### COMMIT 5

Fixtures used: four synthetic UNRATED_MODEL companies at k=7/6/5/4 (3/4/5/6 zeros respectively), two RATED companies with 1 and 2 gaps, one fully-scored RATED company.

**Table 1 (how thin are the unrated), fixture result:** `{"USA": {7: 1, 6: 1, 5: 1, 4: 1}}` - each k-value counted exactly once, correctly.

**Table 2 (lower bar), fixture result:**
| min k | would become rated | stays unrated |
|---|---|---|
| 7 | 1 (k=7 only) | 3 |
| 6 | 2 (k=7,6) | 2 |
| 5 | 3 (k=7,6,5) | 1 |

**Table 3 (where they'd rank at min=6):** only the k=7 and k=6 fixtures appear (k=5,4 correctly excluded); every row's `empty_fields` is always exactly `["inversion_scenario", "inversion_severity", "current_headwind"]` - by construction, since `_parse_one_company()` forces exactly those three to `None` for every NOT RATED row regardless of k (STEP C0.4) - market_structure/one_foot_hurdle/Munger/big_wave are never forced and are simply whatever the model gave, so they're never listed as "empty" here.

**Table 4 (effect on already-rated companies):** the 1-gap fixture (`R1GAP`) appears with `gaps=1`; the fully-scored fixture is correctly absent (0 gaps).

**Table 5 (Top 20 tabs):** reproduced via `top100_render._enriched_pool()`/`_enriched_asx_extension()`/`_sort_and_gate_rated()` directly (no pool was seeded in the fixture test, so all three tabs correctly report 0 members there - exercised separately against a live app-render smoke test with real pool data to confirm no crash).

**Method 2 hand-worked example** (the k=7 fixture, all 7 scored dimensions at score 4, the 3 nulls filled as 3): sum of the 7 scored dimensions' own weights = 68; sum of the 3 null dimensions' own weights = 15 (`DIMENSION_WEIGHT_TOTAL` = 83). Method 2's raw total = `68 * (4-1)/4 + 15 * (3-1)/4` = `68*0.75 + 15*0.5` = `51.0 + 7.5` = `58.5`. Rescaled to 0-100: `58.5 * (100/83)` = `70.48`. The test asserts this hand-worked figure equals the code's own `_composite_score_gap_as_middle()` output - **confirmed equal, 70.48 == 70.48.**

---

## 7. Confirmations

- Both switches (`TOP200_BACKFILL_LIVE`, `TOP200_SCHEMA_MODE`) are **unset** everywhere in the committed code - no default was flipped on.
- System prompt and user prompt text: **unchanged to the character** - `_SYSTEM_PROMPT`/`_user_prompt()` were never touched by any of these five commits (grep-confirmed: zero diff lines touch either).
- With `TOP200_SCHEMA_MODE` unset, the schema is **unchanged** - proven by the request-fingerprint match (Section 3) and the explicit `schema_mode="legacy"` default reproducing the pre-COMMIT-4 `properties` dict construction order byte-for-byte.
- Rubric, the ten dimensions, their anchors and weights, `RUBRIC_VERSION` (**stays "v6"** - never touched), the NOT RATED rule (`NOT_RATED_MIN_NULLS = 3`, unchanged), the ranking-score function (`composite_score()`, behaviorally unchanged - COMMIT 5's refactor only extracted its arithmetic into a helper, verified byte-identical for every existing caller), merit order, public rank order, the persistence rule, and the re-score rules: **all untouched**.
- No stored score was changed or deleted by any of this work - every `top100_store.save_score()`/`record_score_failure()` call added in COMMITs 2/4 is in the ordinary ingest path (only fires on a REAL batch result, never from this session's own test/report work against the real production database - every test in this session ran against an isolated temp-directory SQLite file, `RAILWAY_VOLUME_MOUNT_PATH` pointed at a fresh `tempfile.mkdtemp()` every time, confirmed by the test harness's own setup code).
- No already-scored company is re-sent by anything here - COMMIT 2's re-entry only ever applies to a ticker whose failure was already `_failure_exhausted()`, and COMMIT 3's backfill only ever changes which candidates COUNT toward the 200, never which ones get scored.
- No site quant score enters the prompt - `_SYSTEM_PROMPT`/`_user_prompt()` build the request from `{ticker, company_name, sector}` alone; nothing from `value_score`/MOS/any numeric pipeline output is interpolated into either prompt text, in any of these five commits.
- No Railway variable was touched; nothing was triggered (no scan, no batch, no Refresh All, no recorder run) - every verification in this session ran local fixtures/unit tests only, confirmed via this sandbox's own lack of outbound network access (the one test that DOES attempt a live network call, `test_stage1_japan_commit_a.py`, is pre-existing and unrelated, and simply times out here rather than running).
- **$0 spent** - no Anthropic API call was made by anything in this session (the sandbox cannot reach the Anthropic API at all, per the instruction's own disclosure).

---

## 8. Out-of-scope items noticed

- **ES i18n gap (pre-existing, unrelated to this instruction):** the ES i18n block has no translation at all for `shelf_caption_degenerate_x2` (confirmed still absent). Flagging only, not fixed here.
- **COMMITs 1-4 bundled into one commit** (`8b1acb8`) rather than four separate ones - see Section 3's own note. A future task wanting per-commit `git bisect`/`git revert` granularity for 1-4 specifically would need to treat them as one unit; COMMIT 5 (`fbd9688`) is cleanly separate.
- **Table 3's CSV interpretation:** the task names "a CSV download of the full table" (singular) against five named tables; interpreted as Table 3 (the one table with a complete per-company row covering ticker/company/market/k/unscored dimensions/both methods' rank) - the table that most directly answers "where would they rank." If Andrew wants a different table (or all five) exported, that's a one-line follow-up to `_render_top200_partial_rating_dry_run_panel()`.
- **Table 5's "today's view":** Top 20 tab membership was reproduced using the page's own default sort (`SORT_RESEARCH`) - sort mode changes row ORDER but not which rows are rated, so this is a stable, but not the only possible, choice of "today's" membership for a display-only summary table.
- **Company name fallback:** Tables 3/4 use the CURRENT pool + ASX extension to resolve `ticker -> company_name`; a ticker that was scored in the past but has since left the pool entirely shows its own ticker string in the "Company" column instead (no other name source exists in this engine module).

---

**Status:** all five commits built, tested, and committed locally. Waiting for Andrew's explicit go before any push - earliest 03:00 UTC on 6 October 2026, after the Director has read the 5 Oct 23:00 UTC run.
