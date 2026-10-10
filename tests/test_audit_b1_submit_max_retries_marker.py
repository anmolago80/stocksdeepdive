"""Audit fix B1 / Fable finding T4 (10 Oct 2026, instruction_combined_
10oct.md PART B): top100_engine.submit_nightly_batch()'s own batches.
create() call is non-idempotent - if the request reaches Anthropic's
server and a real batch gets created, but the response never reaches
this process (a network cut, the process killed mid-call), the SDK's
own default retry behaviour would resubmit, creating a SECOND real
batch - doubling real spend with no application-level awareness,
since only one batch.id is ever seen.

Fix: the create() call now runs with max_retries=0 (never auto-
retried - a retry on an ambiguous outcome is exactly the unsafe case).
A "submitting" marker (with the full custom_id_map) is written to
top100_store BEFORE the call. On the VERY NEXT submit_nightly_batch()
call, if that marker is still there (the previous attempt's outcome
was never learned), it's reconciled against batches.list() before any
new submission is considered: if a batch created at/after the marker's
own timestamp is found, it's adopted (never duplicated); if none is
found, the earlier create() call never reached the server at all, and
it's safe to clear the marker and submit fresh.

Run: python3 tests/test_audit_b1_submit_max_retries_marker.py
"""
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TESTVOL = tempfile.mkdtemp(prefix="audit_b1_test_")
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = TESTVOL

import top100_engine as te
import top100_store as ts

if os.path.exists(ts.DB_PATH):
    os.remove(ts.DB_PATH)

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


_POOL = [{"ticker": f"B1T{i}", "company_name": f"B1 Test {i} Co",
          "most_recent_quarter": None} for i in range(3)]
ts.seed_pool_presence({row["ticker"]: 3 for row in _POOL}, "2026-10-09")


# ======================================================================
# CHECK 1: the real create() call is made with max_retries=0 - proven
# via client.with_options(max_retries=0) being called, matching the
# Anthropic SDK's own documented per-call override idiom.
# ======================================================================
with mock.patch("anthropic.Anthropic") as MockClient, \
     mock.patch("anthropic.types.message_create_params.MessageCreateParamsNonStreaming",
                side_effect=lambda **kw: kw), \
     mock.patch("anthropic.types.messages.batch_create_params.Request",
                side_effect=lambda **kw: kw):
    instance = MockClient.return_value
    fake_batch = mock.Mock(id="msgbatch_b1_check1")
    instance.with_options.return_value.messages.batches.create.return_value = fake_batch
    logs1 = []
    result1 = te.submit_nightly_batch(pool=_POOL, log=logs1.append)

check("submit_nightly_batch() calls client.with_options(max_retries=0) "
      "before batches.create() - never the bare client",
      instance.with_options.call_args is not None
      and instance.with_options.call_args.kwargs.get("max_retries") == 0)
check("the batch was submitted successfully (result is the real batch id)",
      result1 == "msgbatch_b1_check1")
check("the submitting marker is cleared after a confirmed-successful create()",
      ts.get_submitting_marker() is None)
check("a real in-flight batch is now tracked", ts.get_batch_state() is not None)
ts.clear_batch_state()


# ======================================================================
# CHECK 2: a create() call whose outcome is ambiguous (raises, e.g. a
# timeout AFTER the request reached the server) leaves the marker in
# place - NOT cleared, NOT treated as a clean failure - since the
# whole point is that this process cannot know whether a real batch
# now exists.
# ======================================================================
if os.path.exists(ts.DB_PATH):
    os.remove(ts.DB_PATH)
ts.seed_pool_presence({row["ticker"]: 3 for row in _POOL}, "2026-10-09")

with mock.patch("anthropic.Anthropic") as MockClient2, \
     mock.patch("anthropic.types.message_create_params.MessageCreateParamsNonStreaming",
                side_effect=lambda **kw: kw), \
     mock.patch("anthropic.types.messages.batch_create_params.Request",
                side_effect=lambda **kw: kw):
    instance2 = MockClient2.return_value
    instance2.with_options.return_value.messages.batches.create.side_effect = (
        ConnectionError("response lost after the request was accepted"))
    logs2 = []
    result2 = te.submit_nightly_batch(pool=_POOL, log=logs2.append)

check("an ambiguous create() failure returns None this call (nothing to report yet)",
      result2 is None)
_marker_after_failure = ts.get_submitting_marker()
check("the submitting marker SURVIVES an ambiguous failure - the outcome is still unknown",
      _marker_after_failure is not None)
check("the surviving marker carries the real custom_id_map for the attempted entrants",
      len(_marker_after_failure["custom_id_map"]) > 0)


# ======================================================================
# CHECK 3: the NEXT submit_nightly_batch() call reconciles the
# surviving marker against batches.list() - a batch found at/after the
# marker's own timestamp is ADOPTED, never duplicated.
# ======================================================================
_found_batch = mock.Mock(id="msgbatch_b1_reconciled",
                          created_at=datetime.now(timezone.utc) + timedelta(seconds=1))
with mock.patch("anthropic.Anthropic") as MockClient3:
    instance3 = MockClient3.return_value
    instance3.messages.batches.list.return_value = [_found_batch]
    logs3 = []
    result3 = te.submit_nightly_batch(pool=_POOL, log=logs3.append)

check("the reconciliation call adopts the found batch's own id, rather than "
      "submitting a brand-new duplicate batch",
      result3 == "msgbatch_b1_reconciled")
check("batches.list() was actually called to check for the earlier attempt",
      instance3.messages.batches.list.called)
check("no NEW batches.create() call happened this round (the found batch was adopted)",
      not instance3.messages.batches.create.called)
check("the marker is cleared once reconciled", ts.get_submitting_marker() is None)
check("the adopted batch is now the one tracked as in-flight",
      ts.get_batch_state()["batch_id"] == "msgbatch_b1_reconciled")
ts.clear_batch_state()


# ======================================================================
# CHECK 4: when NO matching batch is found during reconciliation (the
# earlier create() truly never reached the server), the marker is
# cleared and a FRESH submission proceeds normally.
# ======================================================================
if os.path.exists(ts.DB_PATH):
    os.remove(ts.DB_PATH)
ts.seed_pool_presence({row["ticker"]: 3 for row in _POOL}, "2026-10-09")
ts.save_submitting_marker("2026-10-09", te.MODEL_TOP100, {"t100-0": {"entrants": {"B1T0": {}}}})

with mock.patch("anthropic.Anthropic") as MockClient4, \
     mock.patch("anthropic.types.message_create_params.MessageCreateParamsNonStreaming",
                side_effect=lambda **kw: kw), \
     mock.patch("anthropic.types.messages.batch_create_params.Request",
                side_effect=lambda **kw: kw):
    instance4 = MockClient4.return_value
    # The reconciliation list() call finds nothing matching.
    instance4.messages.batches.list.return_value = []
    fresh_batch = mock.Mock(id="msgbatch_b1_fresh")
    instance4.with_options.return_value.messages.batches.create.return_value = fresh_batch
    logs4 = []
    result4 = te.submit_nightly_batch(pool=_POOL, log=logs4.append)

check("with nothing found during reconciliation, a FRESH batch is submitted",
      result4 == "msgbatch_b1_fresh")
check("the old marker is gone and replaced by the new batch's own in-flight state",
      ts.get_submitting_marker() is None and ts.get_batch_state()["batch_id"] == "msgbatch_b1_fresh")

print()
print(f"PASS={passed} FAIL={failed}")
sys.exit(1 if failed else 0)
