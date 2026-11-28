"""
TASK 81 (Director-directed, owner-approved, 28 Nov 2026) - "What
changed" strip must use the shelf's own status/reason logic.

CONFIRMED BUG (Director-verified, live 28 Nov 2026): top100_render.
_changes_strip()'s own "Awaiting first score" row used to be built from
top-20-by-Value-Score rows where "score_row is None OR score_row.get(
'not_rated')" - lumping a genuinely NOT RATED ticker (the model already
scored it and declined) in with a real AWAITING ticker (never scored at
all), with one generic 5-night tooltip shared by every chip regardless.
The shelf directly below (_shelf_row_html()) already classified the
SAME rows correctly (score_row None -> AWAITING with its own TASK 80
per-ticker reason; score_row present -> NOT RATED) - so SKS.AX/SLDE/
PXED/ORE.AX/SHA.AX/ESP showed "Awaiting first score" in the strip but
"NOT RATED" in the shelf, on the same page, for the same data.

Fix:
  1. _shelf_row_html()'s own chip/caption decision is now a separate
     helper, _shelf_status(row, lang) -> (status, caption) - status is
     "awaiting" or "not_rated". _shelf_row_html() itself just renders
     whatever this returns; its output is confirmed BYTE-IDENTICAL to
     the pre-this-task version (see PART 1 below, a real comparison
     against the git HEAD~0 - i.e. pre-TASK-81 - version of this
     module, not just re-reading the new code).
  2. _changes_strip() uses the SAME helper on the SAME top-20-by-Value-
     Score rows (already carrying presence_nights/score_failure per
     row since TASK 80's own _enriched_pool() enrichment - its one real
     caller already passes exactly that) to build TWO rows instead of
     one: "Awaiting first score (n)" (status "awaiting" only, each
     chip's own tooltip = that ticker's own caption) and a new grey
     "Not rated (n)" row (status "not_rated", same per-ticker tooltip
     convention). Empty rows are omitted, same as New/Dropped always
     were.
  3. No change to scoring, selection, the 5-night rule, rubric, cache
     keys or API calls - confirmed: this task touches only top100_
     render.py (rendering) and 2 new i18n.py strings.

Run: python3 tests/test_director_task81_changes_strip_shelf_status.py
"""
import importlib.util
import os
import subprocess
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

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


import html as _html_mod

import ai_auth_state
import top100_engine as te
import top100_render as tr

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _mk_row(ticker, score_row=None, score_failure=None, presence_nights=1,
            value_score=50.0, mos_pct=None):
    return {"ticker": ticker, "score_row": score_row, "score_failure": score_failure,
            "presence_nights": presence_nights, "value_score": value_score, "mos_pct": mos_pct}


_FIXTURE_ROWS = [
    _mk_row("FOXA", presence_nights=1),                                  # newcomer, night 1 of 5
    _mk_row("NXT", presence_nights=11),                                  # past 5 nights, queued
    _mk_row("XP", presence_nights=9),                                    # past 5 nights, delayed
    _mk_row("RATED_NORMAL", score_row={"not_rated": True, "degenerate_accepted": False}),   # not_rated, plain
    _mk_row("RATED_DEGEN", score_row={"not_rated": True, "degenerate_accepted": True}),     # not_rated, degenerate x2
    _mk_row("EXH", score_failure={"reason": "degenerate_response", "attempts": 99,
                                   "failed_at": "2026-11-20T00:00:00+00:00"}, presence_nights=11),
]


# ======================================================================
# PART 1 (item 1): _shelf_row_html()'s output is BYTE-IDENTICAL to the
# pre-TASK-81 version - a real comparison against the actual git HEAD
# copy of this module (the last commit before this task's own changes),
# not just a re-read of the new source.
# ======================================================================
_old_path = "/tmp/task81_top100_render_before.py"
with open(_old_path, "w") as f:
    subprocess.run(["git", "show", "HEAD:top100_render.py"], cwd=REPO_ROOT, stdout=f, check=True)

_spec = importlib.util.spec_from_file_location("top100_render_before_task81", _old_path)
_old_tr = importlib.util.module_from_spec(_spec)
sys.modules["top100_render_before_task81"] = _old_tr
_spec.loader.exec_module(_old_tr)

_badge = "<span style='display:inline-flex;'>Recently added</span>"
_all_byte_identical = True
for _lang in ("en", "es"):
    for _row in _FIXTURE_ROWS:
        for _failing in (False, True):
            with mock.patch.object(ai_auth_state, "is_failing", return_value=_failing), \
                 mock.patch.object(ai_auth_state, "last_failure_at",
                                    return_value="2026-11-26T23:05:00+00:00"):
                _old_html = _old_tr._shelf_row_html(dict(_row), _lang, recent_badge_html=_badge)
                _new_html = tr._shelf_row_html(dict(_row), _lang, recent_badge_html=_badge)
            if _old_html != _new_html:
                _all_byte_identical = False
                print(f"    MISMATCH {_row['ticker']} lang={_lang} failing={_failing}")
                print(f"      OLD: {_old_html}")
                print(f"      NEW: {_new_html}")
check("_shelf_row_html() output is byte-identical to the pre-TASK-81 git HEAD version "
      "across every fixture row, both languages, both ai_auth states",
      _all_byte_identical)
os.remove(_old_path)


# ======================================================================
# PART 2 (item 1): _shelf_status() itself returns the right (status,
# caption) pair, matching exactly what _shelf_row_html()'s chip variant
# implies (awaiting -> the AWAITING chip; not_rated -> the NOT RATED
# chip) - checked by construction, not by re-deriving the logic.
# ======================================================================
for _row, _expected_status in [
    (_FIXTURE_ROWS[0], "awaiting"), (_FIXTURE_ROWS[1], "awaiting"), (_FIXTURE_ROWS[2], "awaiting"),
    (_FIXTURE_ROWS[3], "not_rated"), (_FIXTURE_ROWS[4], "not_rated"),
    (_FIXTURE_ROWS[5], "awaiting"),  # exhausted failure - still AWAITING chip, "scoring failed" caption
]:
    with mock.patch.object(ai_auth_state, "is_failing", return_value=False):
        status, caption = tr._shelf_status(_row, "en")
        html_row = tr._shelf_row_html(_row, "en")
    check(f"_shelf_status({_row['ticker']}) -> status={_expected_status!r}",
          status == _expected_status)
    check(f"_shelf_status({_row['ticker']})'s caption appears (HTML-escaped) in "
          "_shelf_row_html()'s own output (both read from the exact same source, "
          "same ai_auth_state at the time)",
          _html_mod.escape(caption) in html_row)

# TASK 81a (Director-directed, 28 Nov 2026 - CONFIRMED BUG): _shelf_
# status() itself is NOT changed by this follow-up task (per the
# Director's own instruction) - it still returns "not_rated" for ANY
# row carrying a score_row, including a genuinely RATED one. Documented
# here exactly as the Director verified it live, so this known quirk
# stays visible rather than silently "fixed" by a future edit that
# forgets why _changes_strip() needs its own guard.
_rated_status, _ = tr._shelf_status(
    {"ticker": "X", "score_row": {"not_rated": False, "composite": 80}}, "en")
check("_shelf_status() is unchanged (per instruction) - still returns 'not_rated' for "
      "a genuinely RATED row (score_row present, not_rated=False); this is why "
      "_changes_strip() needs its own guard below, not a fix to _shelf_status() itself",
      _rated_status == "not_rated")


# ======================================================================
# PART 3 (items 2/3): _changes_strip() itself - the 6 real not_rated
# tickers from the Director's own incident report, a VGNT-shaped
# newcomer (night 1 of 5), and one past-5-nights "queued" ticker.
# Confirms the strip's status/tooltip for every ticker equals the
# shelf's own _shelf_status() result for that SAME row - the structural
# guarantee this task exists to establish (one shared classification,
# never two).
#
# TASK 81a (Director-directed, 28 Nov 2026 - CONFIRMED BUG): the 6
# not_rated fixture rows now carry "not_rated": True on their score_row,
# matching real top100_store data (every real score_row has this field)
# - without it, the new skip-already-rated guard in _changes_strip()
# would wrongly treat them as rated and drop them from the "Not rated"
# row entirely. 3 genuinely RATED rows (real score_row, not_rated=False,
# high Value Scores so they land inside the top 20 by construction) are
# added too, proving _changes_strip() no longer lists an actually-rated
# company under "Not rated" - the bug _shelf_status() itself has (it
# returns "not_rated" for ANY row with a score_row, rated or not; safe
# inside the shelf, which only ever sees unrated rows, but not safe over
# the full enriched pool this loop runs over) - fixed by skipping those
# rows in _changes_strip() BEFORE they ever reach _shelf_status().
# ======================================================================
_NOT_RATED_TICKERS = ["SKS.AX", "SLDE", "PXED", "ORE.AX", "SHA.AX", "ESP"]
_RATED_TICKERS = ["RATEDHI1", "RATEDHI2", "RATEDHI3"]
_strip_rows = (
    [_mk_row(t, score_row={"not_rated": True, "degenerate_accepted": False}, value_score=90.0 - i)
     for i, t in enumerate(_NOT_RATED_TICKERS)]
    + [_mk_row("VGNT", presence_nights=1, value_score=80.0)]
    + [_mk_row("QUEUEDCO", presence_nights=11, value_score=79.0)]
    + [_mk_row(t, score_row={"not_rated": False, "composite": 70.0 + i}, value_score=100.0 + i)
       for i, t in enumerate(_RATED_TICKERS)]
)

_captured_markdown = []


def _capture_markdown(text, **kwargs):
    _captured_markdown.append(text)


with mock.patch.object(tr, "st") as _fake_st, \
     mock.patch.object(te, "pool_changes", return_value={"new": [], "dropped": []}), \
     mock.patch.object(ai_auth_state, "is_failing", return_value=False):
    _fake_st.markdown.side_effect = _capture_markdown
    _fake_st.caption.side_effect = lambda *a, **k: None
    tr._changes_strip(_strip_rows, "en")

_strip_html = "".join(_captured_markdown)

check("the strip shows a 'Not rated (6)' row for the 6 genuinely not-rated tickers",
      "Not rated (6)" in _strip_html)
check("the strip shows an 'Awaiting first score (2)' row (VGNT + QUEUEDCO only - "
      "the 6 not-rated tickers are NOT counted here any more)",
      "Awaiting first score (2)" in _strip_html)

for _ticker in _RATED_TICKERS:
    check(f"{_ticker} (genuinely RATED, not_rated=False): does NOT appear as a chip in "
          "the strip at all (TASK 81a fix - skipped before _shelf_status() can "
          "mislabel it 'not_rated')",
          f">{_ticker}<" not in _strip_html)
check("counts stay 'Not rated (6)' even with 3 rated rows sitting in the top 20",
      "Not rated (6)" in _strip_html)
check("counts stay 'Awaiting first score (2)' even with 3 rated rows sitting in the top 20",
      "Awaiting first score (2)" in _strip_html)

for _ticker in _NOT_RATED_TICKERS:
    check(f"{_ticker}: appears as a chip in the strip", f">{_ticker}<" in _strip_html)
    _, _expected_caption = tr._shelf_status(
        next(r for r in _strip_rows if r["ticker"] == _ticker), "en")
    check(f"{_ticker}: the strip chip's own tooltip equals the shelf's own caption for "
          "this exact ticker (the structural guarantee - one shared classification)",
          f"title='{_html_mod.escape(_expected_caption)}'" in _strip_html)

for _ticker, _expect_in_caption in [("VGNT", "night 1 of 5"), ("QUEUEDCO", "queued for tonight")]:
    check(f"{_ticker}: appears as a chip in the strip", f">{_ticker}<" in _strip_html)
    check(f"{_ticker}: its own tooltip names its real, per-ticker reason ({_expect_in_caption!r})",
          _expect_in_caption in _strip_html)


# ======================================================================
# PART 4 (item 2): empty-row omission - a "Not rated" row never renders
# when there are zero not_rated tickers in the top 20 (same convention
# New/Dropped/Awaiting already had).
# ======================================================================
_strip_rows_no_not_rated = [_mk_row("ONLYAWAIT", presence_nights=1, value_score=50.0)]
_captured_markdown.clear()
with mock.patch.object(tr, "st") as _fake_st2, \
     mock.patch.object(te, "pool_changes", return_value={"new": [], "dropped": []}), \
     mock.patch.object(ai_auth_state, "is_failing", return_value=False):
    _fake_st2.markdown.side_effect = _capture_markdown
    _fake_st2.caption.side_effect = lambda *a, **k: None
    tr._changes_strip(_strip_rows_no_not_rated, "en")
_strip_html_no_not_rated = "".join(_captured_markdown)
check('no "Not rated" row at all when zero top-20 tickers are not_rated',
      "Not rated" not in _strip_html_no_not_rated)
check('the "Awaiting first score (1)" row is still present',
      "Awaiting first score (1)" in _strip_html_no_not_rated)

_strip_rows_only_not_rated = [
    _mk_row("ONLYNR", score_row={"not_rated": True, "degenerate_accepted": False}, value_score=50.0)
]
_captured_markdown.clear()
with mock.patch.object(tr, "st") as _fake_st3, \
     mock.patch.object(te, "pool_changes", return_value={"new": [], "dropped": []}), \
     mock.patch.object(ai_auth_state, "is_failing", return_value=False):
    _fake_st3.markdown.side_effect = _capture_markdown
    _fake_st3.caption.side_effect = lambda *a, **k: None
    tr._changes_strip(_strip_rows_only_not_rated, "en")
_strip_html_only_not_rated = "".join(_captured_markdown)
check('no "Awaiting first score" row at all when zero top-20 tickers are awaiting',
      "Awaiting first score" not in _strip_html_only_not_rated)
check('the "Not rated (1)" row is still present',
      "Not rated (1)" in _strip_html_only_not_rated)


# ======================================================================
# PART 5: ES labels.
# ======================================================================
_captured_markdown.clear()
with mock.patch.object(tr, "st") as _fake_st4, \
     mock.patch.object(te, "pool_changes", return_value={"new": [], "dropped": []}), \
     mock.patch.object(ai_auth_state, "is_failing", return_value=False):
    _fake_st4.markdown.side_effect = _capture_markdown
    _fake_st4.caption.side_effect = lambda *a, **k: None
    tr._changes_strip(_strip_rows, "es")
_strip_html_es = "".join(_captured_markdown)
check('ES: "No evaluadas (6)" row label', "No evaluadas (6)" in _strip_html_es)
check('ES: "Esperando primera puntuación (2)" row label',
      "Esperando primera puntuación (2)" in _strip_html_es)


print()
print(f"PASS={passed} FAIL={failed}")
