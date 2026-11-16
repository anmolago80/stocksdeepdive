"""
TASK 66 (Director-directed, owner-approved, 16 Nov 2026) - "Current
headwind" uses recent news.

PROBLEM: OCL.AX (-69% from its Aug 2025 peak, the fall itself ~Aug
2026) showed no "Current headwind" - the Top 200/Quality on sale/Near
the cut-off scoring prompt gave the model NO news/web context at all,
so current_headwind's own honesty rule correctly returned "" rather
than guess.

FIX covered here:
  1. A per-company "Recent news" block (up to 15 headlines, last 90
     days, date+source+title) built from portfolio_news_engine.
     analyze_holding_news() - the SAME feeds/cache the Deep Dive News
     tab's compounder_ui._news_tab_fetch() already reads - and spliced
     into the Top 200/Quality on sale/Near-cut-off batch prompt (all
     three share _user_prompt()/_request_params()). Capped at ~600
     added tokens/company.
  2. current_headwind's own prompt instruction now tells the model to
     ground its answer in that block when it explains the fall; every
     other field/rule is untouched.
  3. RUBRIC_VERSION is NOT bumped (would force a full re-score
     tonight) - instead a new, purely-additive `headwind_news` column
     on top100_scores records whether a given row's own answer had a
     news block to work from.
  4. OCL.AX is queued (top100_store.queue_oneoff_rescore()) for
     tonight's batch regardless of its own normal eligibility.
  5. "Recent news: none found" when no news exists.
  6. A per-batch news/cost log line.

Run: python3 tests/test_director_task66_headwind_news.py
"""
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TESTVOL = tempfile.mkdtemp(prefix="task66_test_")
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = TESTVOL
os.environ.pop("TOP200_QUALITY_V2_LIVE", None)
os.environ.pop("TOP200_MOAT_BLEND_LIVE", None)

passed = 0
failed = 0


def check(label, condition):
    global passed, failed
    if condition:
        passed += 1
        print(f"  OK: {label}")
    else:
        failed += 1
        print(f"  FAIL: {label}")


import portfolio_news_engine
import top100_engine as te
import top100_store as ts

if os.path.exists(ts.DB_PATH):
    os.remove(ts.DB_PATH)


# ======================================================================
# PART A: _format_news_block() - formatting + token cap
# ======================================================================
check("empty list -> the literal 'none found' line (fix #5)",
      te._format_news_block([]) == "  Recent news: none found")

_one_headline = [{"date": datetime(2026, 8, 12), "title": "OCL cuts FY27 guidance after "
                  "weak systems-integration demand", "publisher": "Reuters"}]
_block_one = te._format_news_block(_one_headline)
check("one headline -> header line present",
      _block_one.startswith(f"  Recent news (last {te._HEADWIND_NEWS_WINDOW_DAYS} days):"))
check("one headline -> exact 'date (publisher): title' bullet format",
      "  - 2026-08-12 (Reuters): OCL cuts FY27 guidance after weak systems-integration demand"
      in _block_one)

_undated = [{"date": None, "title": "Some headline", "publisher": "ASX"}]
check("a headline with no date renders as 'undated'",
      "  - undated (ASX): Some headline" in te._format_news_block(_undated))

_no_publisher = [{"date": datetime(2026, 8, 1), "title": "X", "publisher": ""}]
check("a headline with no publisher falls back to 'unknown source'",
      "(unknown source)" in te._format_news_block(_no_publisher))

# Token-cap enforcement: 30 long headlines (well past the real 15-
# headline fetch cap, so this purely exercises _format_news_block()'s
# OWN budget logic in isolation) - the formatted block must never blow
# past the budget by more than the one headline that pushed it over,
# and must always keep at least the header + first headline even if
# that first headline alone exceeds the budget.
_long_headlines = [
    {"date": datetime(2026, 8, 1) - timedelta(days=i),
     "title": "X" * 200, "publisher": "Reuters"}
    for i in range(30)
]
_capped_block = te._format_news_block(_long_headlines)
_budget_chars = te._HEADWIND_NEWS_MAX_TOKENS_PER_COMPANY * te._CHARS_PER_TOKEN_ESTIMATE
check("token cap: 30 long headlines never produce a block anywhere near 30x the "
      "per-headline size - the cap actually bites",
      len(_capped_block) < _budget_chars + 300)
check("token cap: the capped block still keeps at least the header + one headline",
      _capped_block.count("\n  - ") + (1 if _capped_block.startswith("  - ") else 0) >= 1)

_single_huge = [{"date": datetime(2026, 8, 1), "title": "Y" * 5000, "publisher": "Reuters"}]
check("token cap: a single headline that alone exceeds the budget is still kept whole "
      "(never truncated mid-sentence, per the function's own docstring)",
      "Y" * 5000 in te._format_news_block(_single_huge))


# ======================================================================
# PART B: _fetch_recent_news_for_entrant() - relevance/date/sort/cap,
# reusing portfolio_news_engine.analyze_holding_news() directly (no
# Streamlit cache layer, no network - the real feed calls are mocked
# out here).
# ======================================================================
def _mk_event(days_ago, title, relevant=True, now=None):
    now = now or datetime(2026, 11, 16)
    return {"date": now - timedelta(days=days_ago), "title": title, "relevant": relevant,
            "publisher": "Reuters"}


_NOW = datetime(2026, 11, 16)

with mock.patch.object(portfolio_news_engine, "analyze_holding_news",
                        side_effect=Exception("feed down")):
    check("a feed/engine exception never raises - returns [] (fail-soft)",
          te._fetch_recent_news_for_entrant("OCL.AX", now=_NOW) == [])

_events = [
    _mk_event(10, "Relevant recent", now=_NOW),
    _mk_event(10, "Irrelevant recent", relevant=False, now=_NOW),
    _mk_event(200, "Relevant but too old", now=_NOW),
]
with mock.patch.object(portfolio_news_engine, "analyze_holding_news",
                        return_value={"timeline": _events}):
    _fetched = te._fetch_recent_news_for_entrant("OCL.AX", now=_NOW)
check("only the relevant, within-window headline survives", len(_fetched) == 1
      and _fetched[0]["title"] == "Relevant recent")

_many_events = [_mk_event(i, f"Headline {i}", now=_NOW) for i in range(20)]
with mock.patch.object(portfolio_news_engine, "analyze_holding_news",
                        return_value={"timeline": _many_events}):
    _fetched_many = te._fetch_recent_news_for_entrant("OCL.AX", now=_NOW)
check(f"capped at _HEADWIND_NEWS_MAX_HEADLINES ({te._HEADWIND_NEWS_MAX_HEADLINES})",
      len(_fetched_many) == te._HEADWIND_NEWS_MAX_HEADLINES)
check("newest-first, and the kept ones are genuinely the newest",
      [h["title"] for h in _fetched_many] == [f"Headline {i}" for i in range(te._HEADWIND_NEWS_MAX_HEADLINES)])


# ======================================================================
# PART C: _news_fields_for_entrant()
# ======================================================================
with mock.patch.object(portfolio_news_engine, "analyze_holding_news",
                        return_value={"timeline": [_mk_event(5, "A fall-explaining headline", now=_NOW)]}):
    _block, _hn, _count = te._news_fields_for_entrant("OCL.AX", company_name="Objective Corp", now=_NOW)
check("headwind_news=True and count=1 when a headline is found", _hn is True and _count == 1)
check("the block text is the same _format_news_block() would produce",
      "A fall-explaining headline" in _block)

with mock.patch.object(portfolio_news_engine, "analyze_holding_news",
                        return_value={"timeline": []}):
    _block2, _hn2, _count2 = te._news_fields_for_entrant("QUIET", now=_NOW)
check("headwind_news=False and count=0 with no news",
      _hn2 is False and _count2 == 0 and _block2 == "  Recent news: none found")


# ======================================================================
# PART D: _user_prompt() wiring
# ======================================================================
_entrants_with_block = [
    {"ticker": "OCL.AX", "company_name": "Objective Corp", "sector": "Technology",
     "news_block": "  Recent news (last 90 days):\n  - 2026-08-12 (Reuters): Guidance cut"},
]
_prompt_text = te._user_prompt(_entrants_with_block)
check("the entrant's own news_block text is spliced verbatim under its entry",
      "  Recent news (last 90 days):\n  - 2026-08-12 (Reuters): Guidance cut" in _prompt_text)

_entrants_no_block_key = [{"ticker": "NOBLOCK", "company_name": "No Block Co", "sector": "Industrials"}]
check("an entrant with no news_block key at all defaults to 'none found' (back-compat)",
      "  Recent news: none found" in te._user_prompt(_entrants_no_block_key))

check("the intro text tells the model the news block is context ONLY for current_headwind",
      "context ONLY for the current_headwind field" in _prompt_text
      and "never changes any of the ten dimension scores" in _prompt_text)


# ======================================================================
# PART E: schema completely unchanged - no new property anywhere, no
# RUBRIC_VERSION bump.
# ======================================================================
_EXPECTED_ITEM_PROPS = set(te.DIMENSION_KEYS) | {
    "ticker", "inversion_scenario", "inversion_severity", "current_headwind",
    "market_structure", "market_structure_comment", "one_foot_hurdle", "one_foot_comment",
    "munger_quality", "munger_comment", "big_wave", "big_wave_comment",
}
_item_schema = te._company_item_schema()
check("the per-company item schema's property set is byte-identical to the pre-task "
      "v6 set - no 'news'/'recent_news' property added anywhere",
      set(_item_schema["properties"].keys()) == _EXPECTED_ITEM_PROPS)
check("current_headwind is still a plain string field (no union type, no new constraint)",
      _item_schema["properties"]["current_headwind"] == {
          "type": "string",
          "description": _item_schema["properties"]["current_headwind"]["description"],
      } and _item_schema["properties"]["current_headwind"]["type"] == "string")
_resp_schema = te._response_schema()
check("the top-level response schema is still the plain {'companies': [...]} wrapper",
      set(_resp_schema["properties"].keys()) == {"companies"}
      and _resp_schema["properties"]["companies"]["type"] == "array")
check("RUBRIC_VERSION is NOT bumped (still 'v6') - the task's own explicit instruction "
      "since a bump would force a full re-score tonight",
      te.RUBRIC_VERSION == "v6")


# ======================================================================
# PART F: every OTHER part of _SYSTEM_PROMPT is untouched - only the
# CURRENT HEADWIND paragraph gained the new sentence.
# ======================================================================
check("AI EXPOSURE dimension text is byte-identical to before this task",
      "1. AI EXPOSURE - substitution risk vs strengthening." in te._SYSTEM_PROMPT)
check("the de-circularisation principle paragraph is untouched",
      "DE-CIRCULARISATION PRINCIPLE - read this before scoring anything" in te._SYSTEM_PROMPT)
check("the HONESTY RULE paragraph is untouched",
      "HONESTY RULE, the single most important instruction in this prompt" in te._SYSTEM_PROMPT)
check("the inversion synthesis paragraph is untouched",
      "INVERSION SYNTHESIS - after scoring all ten dimensions" in te._SYSTEM_PROMPT)
check("CURRENT HEADWIND gained the new 'base this on the Recent news block' instruction",
      "Base this on the Recent news block below this company's own entry where it "
      "explains the share price fall; cite the specific event" in te._SYSTEM_PROMPT)
check("CURRENT HEADWIND still keeps its original honesty-rule wording unchanged",
      "If the news doesn't explain it and you don't know, output an empty string \"\" "
      "- never invent one." in te._SYSTEM_PROMPT)


# ======================================================================
# PART G: top100_store - headwind_news column + the one-off queue
# ======================================================================
def _save(ticker, headwind_news):
    ts.save_score(
        ticker=ticker, quarter=f"D2026-11-16-{ticker}", model="test-model", rubric_version="v6",
        dims={k: {"score": 4, "justification": "j", "source_period": "FY25"} for k in te.DIMENSION_KEYS},
        not_rated=False, inversion_scenario="", inversion_severity=0,
        prompt="{}", raw_response="{}", headwind_news=headwind_news,
    )


_save("HNTRUE", True)
_row_true = ts.get_score("HNTRUE", "D2026-11-16-HNTRUE", "test-model", "v6")
check("headwind_news=True round-trips as a truthy stored value", bool(_row_true["headwind_news"]))

_save("HNFALSE", False)
_row_false = ts.get_score("HNFALSE", "D2026-11-16-HNFALSE", "test-model", "v6")
check("headwind_news=False round-trips as a falsy (0) stored value",
      _row_false["headwind_news"] is not None and not _row_false["headwind_news"])

_save("HNNONE", None)
_row_none = ts.get_score("HNNONE", "D2026-11-16-HNNONE", "test-model", "v6")
check("headwind_news left unspecified (None) round-trips as NULL/None - a pre-existing "
      "row (or any caller not passing it) reads this, never a guessed 0/1",
      _row_none["headwind_news"] is None)

check("the one-off queue starts empty", ts.pop_oneoff_queue() == [])
ts.queue_oneoff_rescore("OCL.AX")
check("a queued ticker is returned by pop_oneoff_queue()", ts.pop_oneoff_queue() == ["OCL.AX"])
check("popping clears the queue - a second pop returns []", ts.pop_oneoff_queue() == [])
ts.queue_oneoff_rescore("OCL.AX")
ts.queue_oneoff_rescore("OCL.AX")
check("queuing the same ticker twice is idempotent - only one row",
      ts.pop_oneoff_queue() == ["OCL.AX"])
ts.queue_oneoff_rescore("AAA")
ts.queue_oneoff_rescore("BBB")
check("multiple distinct queued tickers both come back", set(ts.pop_oneoff_queue()) == {"AAA", "BBB"})


# ======================================================================
# PART H: submit_nightly_batch() end to end - news wiring, the OCL.AX
# one-off queue, and the per-batch news/cost log line. Mocks the
# Anthropic client (same pattern test_prescoring_promo.py's own
# _submit_and_capture() uses) AND _fetch_recent_news_for_entrant()
# (deterministic, no network).
# ======================================================================
def _mk_pool_row(ticker, company_name=None, sector="Industrials"):
    return {"ticker": ticker, "company_name": company_name or f"{ticker} Co", "sector": sector,
            "most_recent_quarter": None}


def _submit_and_capture(pool, news_by_ticker=None, log=None):
    """Runs submit_nightly_batch() with a mocked Anthropic client and a
    mocked per-ticker news lookup - `news_by_ticker`: {ticker:
    [headline dicts]} (default: no news for anyone)."""
    news_by_ticker = news_by_ticker or {}
    ts.seed_pool_presence({row["ticker"]: te.NEWCOMER_PERSISTENCE_NIGHTS for row in pool},
                           "2026-11-10")

    def _fake_fetch(ticker, name=None, now=None):
        return news_by_ticker.get(ticker, [])

    logs = log if log is not None else []
    with mock.patch("anthropic.Anthropic") as MockClient, \
         mock.patch("anthropic.types.message_create_params.MessageCreateParamsNonStreaming",
                    side_effect=lambda **kw: kw), \
         mock.patch("anthropic.types.messages.batch_create_params.Request",
                    side_effect=lambda **kw: kw), \
         mock.patch.object(te, "_fetch_recent_news_for_entrant", side_effect=_fake_fetch):
        instance = MockClient.return_value
        fake_batch = mock.Mock(id="msgbatch_task66_test")
        instance.with_options.return_value.messages.batches.create.return_value = fake_batch
        result = te.submit_nightly_batch(pool=pool, log=logs.append)
        captured = None
        if instance.with_options.return_value.messages.batches.create.called:
            captured = instance.with_options.return_value.messages.batches.create.call_args.kwargs.get("requests")
        return result, captured, logs


if os.path.exists(ts.DB_PATH):
    os.remove(ts.DB_PATH)
_pool_h = [_mk_pool_row("OCL.AX", "Objective Corp"), _mk_pool_row("QUIET1"), _mk_pool_row("QUIET2")]
_h_logs = []
_result_h, _requests_h, _h_logs = _submit_and_capture(
    _pool_h,
    news_by_ticker={"OCL.AX": [{"date": datetime(2026, 8, 12), "title": "Contract loss drives "
                                 "guidance cut", "publisher": "AFR"}]},
    log=_h_logs,
)
check("submit_nightly_batch() submitted a batch", _result_h == "msgbatch_task66_test")
check("exactly one packed request for the 3-company pool", len(_requests_h) == 1)
_user_text_h = _requests_h[0]["params"]["messages"][0]["content"]
check("OCL.AX's own news headline made it into the real request's user prompt",
      "Contract loss drives guidance cut" in _user_text_h)
check("QUIET1/QUIET2 (no news) render as 'none found' in the real request",
      _user_text_h.count("Recent news: none found") == 2)

_batch_state_h = ts.get_batch_state()
_meta_h = list(_batch_state_h["custom_id_map"].values())[0]["entrants"]
check("OCL.AX's entrant meta carries headwind_news=True", _meta_h["OCL.AX"]["headwind_news"] is True)
check("QUIET1/QUIET2's entrant meta carries headwind_news=False",
      _meta_h["QUIET1"]["headwind_news"] is False and _meta_h["QUIET2"]["headwind_news"] is False)

check("the per-batch news/cost log line fires with the right 1/3 count",
      any(m.startswith("[top100] headwind news: 1/3 companies had news (") and "extra est. cost" in m
          for m in _h_logs))

with ts._conn() as conn:
    conn.execute("DELETE FROM top100_daily_submissions")

# --- One-off queue: OCL.AX itself is ALREADY scored under the current
# rubric (so normal selection would NOT pick it up on its own - this
# is the real scenario the task describes, re-reading a company that
# already has a fresh score) and the pool's only other ticker is also
# already scored, so without the queue this would be a "nothing to
# submit" night. Proves the one-off queue genuinely forces a ticker in
# that normal eligibility would otherwise skip. ---
if os.path.exists(ts.DB_PATH):
    os.remove(ts.DB_PATH)
_pool_oneoff = [_mk_pool_row("OCL.AX", "Objective Corp"), _mk_pool_row("ALREADYSCORED")]
ts.seed_pool_presence({row["ticker"]: te.NEWCOMER_PERSISTENCE_NIGHTS for row in _pool_oneoff},
                       "2026-11-10")
for _t in ("OCL.AX", "ALREADYSCORED"):
    ts.save_score(
        ticker=_t, quarter=f"RPsome-{_t}", model=te.MODEL_TOP100, rubric_version=te.RUBRIC_VERSION,
        dims={k: {"score": 4, "justification": "j", "source_period": "FY25"} for k in te.DIMENSION_KEYS},
        not_rated=False, inversion_scenario="", inversion_severity=0, prompt="{}", raw_response="{}",
    )
check("sanity: with nothing queued, a night where everything is already scored submits nothing",
      _submit_and_capture(_pool_oneoff, news_by_ticker={}, log=[])[0] is None)
ts.queue_oneoff_rescore("OCL.AX")
_oneoff_logs = []
_result_oneoff, _requests_oneoff, _oneoff_logs = _submit_and_capture(
    _pool_oneoff, news_by_ticker={}, log=_oneoff_logs)
check("queuing OCL.AX makes an otherwise-nothing-to-submit night submit a batch",
      _result_oneoff == "msgbatch_task66_test")
_oneoff_batch_state = ts.get_batch_state()
_oneoff_entrants = list(_oneoff_batch_state["custom_id_map"].values())[0]["entrants"]
check("OCL.AX is the ONLY entrant submitted - already-scored ALREADYSCORED is still "
      "correctly left out (the queue adds ONLY the ticker it names)",
      set(_oneoff_entrants.keys()) == {"OCL.AX"})
check("the one-off queue log line names OCL.AX",
      any("[top100] one-off queue: OCL.AX added to tonight's batch" == m for m in _oneoff_logs))
check("the queue is empty again after this submission claimed it",
      ts.pop_oneoff_queue() == [])

with ts._conn() as conn:
    conn.execute("DELETE FROM top100_daily_submissions")

# --- One-off queue: a queued ticker outside tonight's pool is skipped,
# not a crash, and doesn't block the rest of the batch. ---
if os.path.exists(ts.DB_PATH):
    os.remove(ts.DB_PATH)
_pool_normal = [_mk_pool_row("NORMALX")]
ts.queue_oneoff_rescore("NOTINPOOL")
_skip_logs = []
_result_skip, _requests_skip, _skip_logs = _submit_and_capture(
    _pool_normal, news_by_ticker={}, log=_skip_logs)
check("a queued ticker outside the pool never raises - the batch still submits",
      _result_skip == "msgbatch_task66_test")
check("the skip is logged by name", any("NOTINPOOL not in tonight's pool - skipped" in m
                                          for m in _skip_logs))
check("the normal entrant (NORMALX) still submits despite the unrelated skip",
      set(list(ts.get_batch_state()["custom_id_map"].values())[0]["entrants"].keys()) == {"NORMALX"})
check("NOTINPOOL's queue entry was still consumed (not left to loop forever)",
      ts.pop_oneoff_queue() == [])

with ts._conn() as conn:
    conn.execute("DELETE FROM top100_daily_submissions")


# ======================================================================
# PART I: headwind_news flows all the way through to the stored score
# via poll_and_ingest_batch() (using a directly-seeded batch_state, the
# same pattern test_director_task48_sticky_buffer_and_near_cutoff.py's
# own ingest checks use).
# ======================================================================
if os.path.exists(ts.DB_PATH):
    os.remove(ts.DB_PATH)
ts.save_submitting_marker("2026-11-16", te.MODEL_TOP100, {})
ts.clear_submitting_marker()
ts.save_batch_state("msgbatch_task66_ingest_test", "2026-11-16", te.MODEL_TOP100, {
    "t100-0": {
        "entrants": {
            "OCL.AX": {"score_key": "D2026-11-16", "most_recent_quarter": None, "headwind_news": True},
            "QUIETX": {"score_key": "D2026-11-16", "most_recent_quarter": None, "headwind_news": False},
        },
        "schema_mode": "legacy", "is_prescore": False, "is_quality_on_sale": False, "is_near_cutoff": False,
    },
})


def _mk_ingest_result(custom_id, tickers):
    companies = []
    for t in tickers:
        item = {k: {"score": 4, "justification": "j", "source_period": "FY25"} for k in te.DIMENSION_KEYS}
        item.update({
            "ticker": t, "inversion_scenario": "", "inversion_severity": 0,
            "current_headwind": "Contract loss drove guidance down" if t == "OCL.AX" else "",
            "market_structure": "", "market_structure_comment": "",
            "one_foot_hurdle": "", "one_foot_comment": "",
            "munger_quality": "", "munger_comment": "",
            "big_wave": "", "big_wave_comment": "",
        })
        companies.append(item)
    msg = mock.Mock()
    msg.usage = mock.Mock(input_tokens=1000, output_tokens=500,
                           cache_creation_input_tokens=0, cache_read_input_tokens=0)
    msg.content = [mock.Mock(type="text", text=json.dumps({"companies": companies}))]
    msg.stop_reason = "end_turn"
    result = mock.Mock()
    result.custom_id = custom_id
    result.result = mock.Mock(type="succeeded", message=msg)
    return result


with mock.patch("anthropic.Anthropic") as MockClient:
    instance = MockClient.return_value
    instance.messages.batches.retrieve.return_value = mock.Mock(processing_status="ended")
    instance.messages.batches.results.return_value = [
        _mk_ingest_result("t100-0", ["OCL.AX", "QUIETX"]),
    ]
    _ingest_summary_i = te.poll_and_ingest_batch(log=lambda *a, **k: None)

check("both results ingested", _ingest_summary_i is not None and _ingest_summary_i["scored"] == 2)
_ocl_row = ts.latest_score_for_ticker("OCL.AX", te.MODEL_TOP100)
_quiet_row = ts.latest_score_for_ticker("QUIETX", te.MODEL_TOP100)
check("OCL.AX's stored row carries headwind_news=True (as submitted)",
      bool(_ocl_row["headwind_news"]))
check("OCL.AX's stored current_headwind is the news-grounded answer",
      _ocl_row["current_headwind"] == "Contract loss drove guidance down")
check("QUIETX's stored row carries headwind_news=False (as submitted)",
      _quiet_row["headwind_news"] is not None and not _quiet_row["headwind_news"])


# ======================================================================
# PART J: run_single_test_call() wiring - the diagnostic single-call
# path shares the same news lookup.
# ======================================================================
if os.path.exists(ts.DB_PATH):
    os.remove(ts.DB_PATH)


def _mk_single_resp(ticker):
    item = {k: {"score": 4, "justification": "j", "source_period": "FY25"} for k in te.DIMENSION_KEYS}
    item.update({
        "ticker": ticker, "inversion_scenario": "", "inversion_severity": 0,
        "current_headwind": "Guidance cut after a contract loss",
        "market_structure": "", "market_structure_comment": "",
        "one_foot_hurdle": "", "one_foot_comment": "",
        "munger_quality": "", "munger_comment": "", "big_wave": "", "big_wave_comment": "",
    })
    resp = mock.Mock()
    resp.usage = mock.Mock(input_tokens=1200, output_tokens=400)
    resp.content = [mock.Mock(type="text", text=json.dumps({"companies": [item]}))]
    return resp


with mock.patch("anthropic.Anthropic") as MockClient, \
     mock.patch.object(te, "_fetch_recent_news_for_entrant",
                        return_value=[{"date": datetime(2026, 8, 12), "title": "Guidance cut after "
                                        "a contract loss", "publisher": "AFR"}]):
    instance = MockClient.return_value
    instance.messages.create.return_value = _mk_single_resp("OCL.AX")
    _single_result = te.run_single_test_call("OCL.AX", company_name="Objective Corp")

check("run_single_test_call()'s own request carried the news block too",
      "Guidance cut after a contract loss" in
      instance.messages.create.call_args.kwargs["messages"][0]["content"])
check("the returned dict reports headwind_news=True",
      _single_result["headwind_news"] is True)
_single_stored = ts.latest_score_for_ticker("OCL.AX", te.MODEL_TOP100)
check("the saved row from run_single_test_call() also carries headwind_news=True",
      bool(_single_stored["headwind_news"]))


# ======================================================================
if os.path.exists(ts.DB_PATH):
    os.remove(ts.DB_PATH)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
