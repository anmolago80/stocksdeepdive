"""
TASK 80 (Director-directed, owner-approved, 27 Nov 2026) - AI key
failure: test button, alert, real "awaiting" reasons, layout fix.

CONTEXT (verified in Railway logs): every nightly Top 200 AI batch from
21 to 26 Nov 2026 failed with "401 invalid x-api-key" (submit AND
pending-batch reconcile). Nothing alerted the owner for 6 nights.
Selection by Value Score kept running, so 12 companies were "Awaiting"
and the page told them all "scored once it has held a pool slot for 5
consecutive nights", which was wrong for those already past 5 nights.

This file covers:
  1. ai_auth_state.py's own state machine (record_failure/record_
     success/is_failing/banner_message/ai_auth_ok/last_failure_at),
     the once-per-UTC-day email dedup, and that the API key is NEVER
     logged or included in any message this module produces.
  2. test_ai_key()'s OK and 401/403 paths (mocked Anthropic client),
     its own exact "[ai_auth] test <OK|FAILED status>" log line.
  3. top100_engine.py's 3 real Anthropic call sites (submit/poll/
     reconcile) correctly detecting a 401/403 and recording it, and a
     success clearing a prior failure.
  4. Item 5 - a stale submitting-marker left by a FAILED create() call
     (simulating the real incident) is correctly reconciled once a
     VALID key is back: batches.list() finds no matching batch (none
     was ever created), the marker is cleared, and that SAME call
     proceeds to submit a fresh batch exactly once (no double
     submission, no permanent skip) - confirming by test, not just by
     reading, that the EXISTING reconcile logic already handles this
     correctly for the real incident shape.
  5. _log_coverage()'s new ai_auth_ok=<True|False> field.
  6. The 3 real AWAITING reasons in top100_render._shelf_row_html()
     (newcomer/queued/delayed), rendered from WMS-shaped fixtures
     matching the Director's own NXT/XP (past 5 nights)/FOXA (night 1)
     examples.
  7. The layout fix - a row carrying a "Recently added" badge keeps its
     reason text on the same line (flex-wrap:nowrap, not wrap).
  8. Admin panel wiring (app.py) - source-level confirmation the "Test
     AI key" button and the red banner are actually wired in, under
     the same owner gate as "Backup now".

Run: python3 tests/test_director_task80_ai_key_failure_handling.py
"""
import inspect
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TESTVOL = tempfile.mkdtemp(prefix="task80_test_")
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = TESTVOL

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


import ai_auth_state
import top100_engine as te
import top100_store as ts
import top100_render as tr

if os.path.exists(ts.DB_PATH):
    os.remove(ts.DB_PATH)


def _fresh_state():
    if os.path.exists(ai_auth_state.STATE_PATH):
        os.remove(ai_auth_state.STATE_PATH)


# ======================================================================
# PART 1: the state machine itself.
# ======================================================================
_fresh_state()
_logs = []
check("fresh state: not failing, no banner, ai_auth_ok True",
      not ai_auth_state.is_failing() and ai_auth_state.banner_message() is None
      and ai_auth_state.ai_auth_ok() is True)

with mock.patch.object(ai_auth_state, "_maybe_send_alert_email"):
    ai_auth_state.record_failure("top100_submit", 401, log=_logs.append)
check('record_failure logs exactly "[ai_auth] FAILED <feature> <status>"',
      "[ai_auth] FAILED top100_submit 401" in _logs)
check("after a failure: is_failing() True, ai_auth_ok() False",
      ai_auth_state.is_failing() and ai_auth_state.ai_auth_ok() is False)
check('banner_message() names "AI scoring failing since <time>"',
      (ai_auth_state.banner_message() or "").startswith("AI scoring failing since "))
check("last_failure_at() is set to a real ISO timestamp",
      ai_auth_state.last_failure_at() is not None)

# A second failure the same streak does NOT move first_failure_at.
_first_failure_at = ai_auth_state._load_state()["first_failure_at"]
with mock.patch.object(ai_auth_state, "_maybe_send_alert_email"):
    ai_auth_state.record_failure("top100_reconcile", 403, log=_logs.append)
check("a second failure in the same streak keeps the ORIGINAL first_failure_at "
      "(the banner names when it STARTED, not the latest failure)",
      ai_auth_state._load_state()["first_failure_at"] == _first_failure_at)
check("last_status/last_failure_at DO update to the most recent failure",
      ai_auth_state._load_state()["last_status"] == 403)

ai_auth_state.record_success(log=_logs.append)
check("record_success() clears is_failing()/ai_auth_ok()/banner_message()",
      not ai_auth_state.is_failing() and ai_auth_state.ai_auth_ok() is True
      and ai_auth_state.banner_message() is None)
check("record_success() keeps last_failure_at as history (for a still-waiting "
      "ticker's own 'last run failed' caption even after the key is fixed)",
      ai_auth_state.last_failure_at() is not None)


# ======================================================================
# PART 1b: auth_error_status() only recognises 401/403, never anything
# else (a network error, a 500, a rate limit).
# ======================================================================
class _FakeErr(Exception):
    def __init__(self, status_code):
        self.status_code = status_code


check("auth_error_status(401) -> 401", ai_auth_state.auth_error_status(_FakeErr(401)) == 401)
check("auth_error_status(403) -> 403", ai_auth_state.auth_error_status(_FakeErr(403)) == 403)
check("auth_error_status(500) -> None (not an auth error)",
      ai_auth_state.auth_error_status(_FakeErr(500)) is None)
check("auth_error_status(a plain exception with no status_code) -> None",
      ai_auth_state.auth_error_status(ConnectionError("network down")) is None)


# ======================================================================
# PART 2: once-per-UTC-day email dedup + the key is NEVER logged.
# ======================================================================
_fresh_state()
_sent = []


def _fake_send(status):
    _sent.append(status)


with mock.patch.object(ai_auth_state, "_send_alert_email", side_effect=_fake_send):
    ai_auth_state.record_failure("top100_submit", 401, log=lambda *a: None)
    ai_auth_state.record_failure("top100_reconcile", 401, log=lambda *a: None)
    ai_auth_state.record_failure("ai_watchdog", 401, log=lambda *a: None)
check("3 separate failures the SAME UTC day send exactly ONE alert email",
      len(_sent) == 1)

_fresh_state()
_sent2 = []
with mock.patch.object(ai_auth_state, "_send_alert_email", side_effect=lambda s: _sent2.append(s)):
    ai_auth_state.record_failure("top100_submit", 401, log=lambda *a: None)
    # Simulate the next UTC day by rewriting last_email_sent_date directly
    # (cheaper/more direct than monkeypatching datetime.now everywhere
    # this module calls it).
    _st = ai_auth_state._load_state()
    _st["last_email_sent_date"] = (datetime.now(timezone.utc).date() - timedelta(days=1)).isoformat()
    ai_auth_state._save_state(_st)
    ai_auth_state.record_failure("top100_submit", 401, log=lambda *a: None)
check("a failure on a LATER UTC day sends a second alert email",
      len(_sent2) == 2)

_fresh_state()
_sent3 = []
with mock.patch.object(ai_auth_state, "_send_alert_email", side_effect=lambda s: _sent3.append(s)):
    ai_auth_state.record_failure("top100_submit", 401, log=lambda *a: None)
    ai_auth_state.record_success(log=lambda *a: None)
    ai_auth_state.record_failure("top100_submit", 401, log=lambda *a: None)
check("a NEW failure streak after a recovery (same UTC day) emails again "
      "immediately, rather than waiting for the next calendar day",
      len(_sent3) == 2)

# The key itself is never logged, anywhere in this module - sanity
# check with a distinctive fake key in the environment.
_fresh_state()
_FAKE_KEY = "sk-ant-totally-fake-task80-canary-key-should-never-appear-anywhere"
with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": _FAKE_KEY}), \
     mock.patch.object(ai_auth_state, "_maybe_send_alert_email"):
    _canary_logs = []
    ai_auth_state.record_failure("top100_submit", 401, log=_canary_logs.append)
    _ok_canary, _msg_canary = (False, "")
    try:
        with mock.patch("anthropic.Anthropic") as MockClient:
            MockClient.return_value.models.list.side_effect = _FakeErr(401)
            _ok_canary, _msg_canary = ai_auth_state.test_ai_key(log=_canary_logs.append)
    except Exception:
        pass
check("the fake API key string never appears in any log line this module produced",
      all(_FAKE_KEY not in str(ln) for ln in _canary_logs))
check("the fake API key string never appears in the test_ai_key() message either",
      _FAKE_KEY not in _msg_canary)


# ======================================================================
# PART 3 (item 1): test_ai_key() - OK and 401/403 paths.
# ======================================================================
_fresh_state()
_logs_ok = []
with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-test-fake-for-mock"}), \
     mock.patch("anthropic.Anthropic") as MockClientOK:
    MockClientOK.return_value.models.list.return_value = []
    ok, msg = ai_auth_state.test_ai_key(log=_logs_ok.append)
check("test_ai_key() OK path: returns (True, 'AI key OK at <time>')",
      ok and msg.startswith("AI key OK at "))
check('test_ai_key() OK path logs exactly "[ai_auth] test OK"',
      "[ai_auth] test OK" in _logs_ok)
check("a successful test_ai_key() clears is_failing()", not ai_auth_state.is_failing())

_fresh_state()
_logs_401 = []
with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-test-fake-for-mock"}), \
     mock.patch("anthropic.Anthropic") as MockClient401, \
     mock.patch.object(ai_auth_state, "_maybe_send_alert_email"):
    MockClient401.return_value.models.list.side_effect = _FakeErr(401)
    ok401, msg401 = ai_auth_state.test_ai_key(log=_logs_401.append)
check("test_ai_key() 401 path: returns (False, 'AI key REJECTED (401) at <time>')",
      not ok401 and "AI key REJECTED (401)" in msg401)
check('test_ai_key() 401 path logs "[ai_auth] test FAILED 401"',
      "[ai_auth] test FAILED 401" in _logs_401)
check("a rejected test_ai_key() leaves is_failing() True", ai_auth_state.is_failing())

_fresh_state()
_logs_nokey = []
with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": ""}):
    ok_nk, msg_nk = ai_auth_state.test_ai_key(log=_logs_nokey.append)
check("test_ai_key() with no key configured at all: returns False, names 'no "
      "ANTHROPIC_API_KEY configured'", not ok_nk and "no ANTHROPIC_API_KEY configured" in msg_nk)


# ======================================================================
# PART 4 (items 3/4): top100_engine.py's 3 real call sites.
# ======================================================================
_POOL = [{"ticker": f"T80P{i}", "company_name": f"Task80 Pool {i}",
          "most_recent_quarter": None} for i in range(3)]
ts.seed_pool_presence({row["ticker"]: te.NEWCOMER_PERSISTENCE_NIGHTS for row in _POOL}, "2026-11-20")

# 4a. submit_nightly_batch()'s create() hitting a 401.
_fresh_state()
with mock.patch("anthropic.Anthropic") as MockClientSub, \
     mock.patch("anthropic.types.message_create_params.MessageCreateParamsNonStreaming",
                side_effect=lambda **kw: kw), \
     mock.patch("anthropic.types.messages.batch_create_params.Request",
                side_effect=lambda **kw: kw), \
     mock.patch.object(ai_auth_state, "_maybe_send_alert_email") as _mock_email_sub:
    MockClientSub.return_value.with_options.return_value.messages.batches.create.side_effect = _FakeErr(401)
    _logs_sub = []
    _r = te.submit_nightly_batch(pool=_POOL, log=_logs_sub.append)
check("submit_nightly_batch(): a 401 on create() is logged via ai_auth_state "
      '("[ai_auth] FAILED top100_submit 401")',
      "[ai_auth] FAILED top100_submit 401" in _logs_sub)
check("submit_nightly_batch(): the once-per-day alert email path was triggered",
      _mock_email_sub.called)
check("submit_nightly_batch(): the banner is now showing (ai_auth_state.is_failing())",
      ai_auth_state.is_failing())
ts.clear_submitting_marker()
ts.clear_batch_state()

# 4b. poll_and_ingest_batch()'s retrieve() hitting a 401.
if os.path.exists(ts.DB_PATH):
    os.remove(ts.DB_PATH)
ts.save_batch_state("msgbatch_task80_poll", te.MODEL_TOP100, "2026-11-20", {})
_fresh_state()
with mock.patch("anthropic.Anthropic") as MockClientPoll, \
     mock.patch.object(ai_auth_state, "_maybe_send_alert_email"):
    MockClientPoll.return_value.messages.batches.retrieve.side_effect = _FakeErr(401)
    _logs_poll = []
    te.poll_and_ingest_batch(log=_logs_poll.append)
check("poll_and_ingest_batch(): a 401 on retrieve() is logged via ai_auth_state "
      '("[ai_auth] FAILED top100_reconcile 401")',
      "[ai_auth] FAILED top100_reconcile 401" in _logs_poll)
ts.clear_batch_state()

# 4c. a SUCCESS on either path clears a prior failure (banner off).
if os.path.exists(ts.DB_PATH):
    os.remove(ts.DB_PATH)
ts.save_batch_state("msgbatch_task80_poll2", te.MODEL_TOP100, "2026-11-20", {})
_fresh_state()
with mock.patch.object(ai_auth_state, "_maybe_send_alert_email"):
    ai_auth_state.record_failure("top100_submit", 401, log=lambda *a: None)
check("sanity: banner is on before the successful retrieve()", ai_auth_state.is_failing())
_fake_batch_still_processing = mock.Mock(processing_status="in_progress")
with mock.patch("anthropic.Anthropic") as MockClientPollOK:
    MockClientPollOK.return_value.messages.batches.retrieve.return_value = _fake_batch_still_processing
    te.poll_and_ingest_batch(log=lambda *a: None)
check("a SUCCESSFUL retrieve() (even if the batch itself is still processing) "
      "clears the banner - the auth succeeded even though scoring isn't done yet",
      not ai_auth_state.is_failing())
ts.clear_batch_state()


# ======================================================================
# PART 5 (item 5): a stale submitting-marker left by a 401-failed
# create() call is correctly reconciled once a VALID key is back -
# batches.list() finds nothing (none was ever created), the marker is
# cleared, and THIS SAME submit_nightly_batch() call proceeds to
# submit a fresh batch exactly once. Confirms the EXISTING reconcile
# logic already handles the real incident shape correctly - no fix
# needed, just this test.
# ======================================================================
if os.path.exists(ts.DB_PATH):
    os.remove(ts.DB_PATH)
ts.seed_pool_presence({row["ticker"]: te.NEWCOMER_PERSISTENCE_NIGHTS for row in _POOL}, "2026-11-20")
# Night 1: create() fails with 401 - the marker is written BEFORE the
# call (submit_nightly_batch()'s own design) and survives since the
# outcome is ambiguous to this process.
_fresh_state()
with mock.patch("anthropic.Anthropic") as MockClientN1, \
     mock.patch("anthropic.types.message_create_params.MessageCreateParamsNonStreaming",
                side_effect=lambda **kw: kw), \
     mock.patch("anthropic.types.messages.batch_create_params.Request",
                side_effect=lambda **kw: kw), \
     mock.patch.object(ai_auth_state, "_maybe_send_alert_email"):
    MockClientN1.return_value.with_options.return_value.messages.batches.create.side_effect = _FakeErr(401)
    te.submit_nightly_batch(pool=_POOL, log=lambda *a: None)
_stale_marker = ts.get_submitting_marker()
check("night 1 (401 on create): the submitting marker survives (outcome unknown)",
      _stale_marker is not None)
check("night 1: ai_auth_state is now failing", ai_auth_state.is_failing())

# Night 2: the owner has replaced the key. This call's batches.list()
# (the reconcile check) now SUCCEEDS and finds nothing (no real batch
# was ever created the previous night) -> marker cleared -> THIS call
# falls through and submits a fresh batch, exactly once.
with mock.patch("anthropic.Anthropic") as MockClientN2, \
     mock.patch("anthropic.types.message_create_params.MessageCreateParamsNonStreaming",
                side_effect=lambda **kw: kw), \
     mock.patch("anthropic.types.messages.batch_create_params.Request",
                side_effect=lambda **kw: kw):
    instance_n2 = MockClientN2.return_value
    instance_n2.messages.batches.list.return_value = []  # nothing found - never created
    _fresh_batch = mock.Mock(id="msgbatch_task80_recovered")
    instance_n2.with_options.return_value.messages.batches.create.return_value = _fresh_batch
    _logs_n2 = []
    _result_n2 = te.submit_nightly_batch(pool=_POOL, log=_logs_n2.append)
check("night 2 (valid key): batches.list() was called to reconcile the stale marker",
      instance_n2.messages.batches.list.called)
check("night 2: the stale marker is cleared (no matching batch ever reached the server)",
      ts.get_submitting_marker() is None)
check("night 2: a FRESH batch is submitted - exactly once, no double submission",
      _result_n2 == "msgbatch_task80_recovered"
      and instance_n2.with_options.return_value.messages.batches.create.call_count == 1)
check("night 2: ai_auth_state reflects the recovery (reconcile's own list() call "
      "succeeded, clearing is_failing())",
      not ai_auth_state.is_failing())
ts.clear_batch_state()


# ======================================================================
# PART 6 (item 4): _log_coverage()'s new ai_auth_ok=<True|False> field.
# ======================================================================
if os.path.exists(ts.DB_PATH):
    os.remove(ts.DB_PATH)
_fresh_state()
_logs_cov_ok = []
te._log_coverage([], log=_logs_cov_ok.append)
_cov_line_ok = next((ln for ln in _logs_cov_ok if ln.startswith("[top100] coverage")), None)
check("[top100] coverage line exists and includes ai_auth_ok=True while the key is healthy",
      _cov_line_ok is not None and "ai_auth_ok=True" in _cov_line_ok)

with mock.patch.object(ai_auth_state, "_maybe_send_alert_email"):
    ai_auth_state.record_failure("top100_submit", 401, log=lambda *a: None)
_logs_cov_fail = []
te._log_coverage([], log=_logs_cov_fail.append)
_cov_line_fail = next((ln for ln in _logs_cov_fail if ln.startswith("[top100] coverage")), None)
check("the SAME coverage line includes ai_auth_ok=False while the key is failing",
      _cov_line_fail is not None and "ai_auth_ok=False" in _cov_line_fail)
_fresh_state()


# ======================================================================
# PART 7 (item 6): the 3 real AWAITING reasons, matching the Director's
# own NXT/XP (past 5 nights)/FOXA (night 1) examples.
# ======================================================================
def _mk_row(ticker, presence_nights):
    return {"ticker": ticker, "score_row": None, "score_failure": None,
            "presence_nights": presence_nights, "value_score": 42.0, "mos_pct": None}


_fresh_state()  # ai_auth healthy
_html_foxa = tr._shelf_row_html(_mk_row("FOXA", 1), "en")
check('FOXA (night 1 of 5, held < 5 nights): "scored after 5 nights in the pool '
      '(night 1 of 5)"',
      "scored after 5 nights in the pool (night 1 of 5)" in _html_foxa)

_html_nxt = tr._shelf_row_html(_mk_row("NXT", 11), "en")
check('NXT (11 nights held, past 5, key healthy): "queued for tonight\'s scoring run"',
      "queued for tonight&#x27;s scoring run" in _html_nxt or "queued for tonight's scoring run" in _html_nxt)

with mock.patch.object(ai_auth_state, "_maybe_send_alert_email"):
    ai_auth_state.record_failure("top100_submit", 401, log=lambda *a: None)
_html_xp = tr._shelf_row_html(_mk_row("XP", 9), "en")
check('XP (9 nights held, past 5, key currently FAILING): "scoring delayed - last '
      'run failed (<date>)"',
      "scoring delayed - last run failed (" in _html_xp)
_today_str = datetime.now(timezone.utc).date().isoformat()
check("XP's caption names today's own date (the failure just recorded above)",
      _today_str in _html_xp)
_fresh_state()

# Pre-existing, UNCHANGED case: an EXHAUSTED per-ticker failure still
# shows its own distinct caption regardless of nights held or ai_auth
# state - this task never touches that branch.
_exhausted_failure = {"reason": "degenerate_response", "attempts": 99,
                      "failed_at": "2026-11-20T00:00:00+00:00"}
with mock.patch.object(te, "_failure_exhausted", return_value=True):
    _row_exhausted = {**_mk_row("EXH", 11), "score_failure": _exhausted_failure}
    _html_exhausted = tr._shelf_row_html(_row_exhausted, "en")
check("a ticker with an EXHAUSTED per-rubric failure still shows the pre-existing "
      '"scoring failed" caption, unaffected by this task\'s 3 new reasons',
      "scoring failed" in _html_exhausted and "queued for tonight" not in _html_exhausted
      and "scoring delayed" not in _html_exhausted)


# ======================================================================
# PART 8 (item 7): the layout fix - a row with a "Recently added" badge
# keeps its reason text on the SAME line.
# ======================================================================
_badge_html = "<span style='display:inline-flex;padding:2px 8px;'>Recently added (24 Nov)</span>"
for _badge_ticker, _nights in (("IESC", 1), ("VIRT", 6), ("AII", 2), ("FOXA", 1), ("ESP", 7)):
    _html_badge = tr._shelf_row_html(_mk_row(_badge_ticker, _nights), "en", recent_badge_html=_badge_html)
    check(f"{_badge_ticker} (with Recently added badge): row container is "
          "flex-wrap:nowrap (never wrap), so the reason can't be pushed to its own line",
          "flex-wrap:nowrap" in _html_badge and "flex-wrap:wrap" not in _html_badge)
    check(f"{_badge_ticker}: the reason span itself is present in the SAME returned "
          "HTML string as the badge (one row, one line - no second <div> ever created)",
          _badge_html in _html_badge and "<span style='color:#5b7290" in _html_badge)


# ======================================================================
# PART 9 (item 8): admin panel wiring (app.py) - source-level, since
# this file has no Streamlit runtime to actually click the button in.
# ======================================================================
with open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app.py")) as f:
    _app_src = f.read()
check('app.py: a "Test AI key" button exists', '"Test AI key"' in _app_src)
check("app.py: the button calls ai_auth_state.test_ai_key()",
      "ai_auth_state.test_ai_key()" in _app_src)
check("app.py: the red banner is wired to ai_auth_state.banner_message()",
      "ai_auth_state.banner_message()" in _app_src)
# Same owner gate: the new block must appear inside _render_compounder_
# admin_panel(), textually AFTER that function's own ADMIN_REFRESH_KEY
# gate check, and BEFORE the pre-existing "Backup now" button (same
# popover, same gate, no separate access control introduced).
_panel_start = _app_src.index("def _render_compounder_admin_panel(")
_backup_now_idx = _app_src.index('"Backup now"', _panel_start)
_test_key_idx = _app_src.index('"Test AI key"', _panel_start)
check('app.py: the "Test AI key" button is defined INSIDE _render_compounder_'
      'admin_panel() and BEFORE "Backup now" (same function, same owner gate)',
      _panel_start < _test_key_idx < _backup_now_idx)


print()
print(f"PASS={passed} FAIL={failed}")
