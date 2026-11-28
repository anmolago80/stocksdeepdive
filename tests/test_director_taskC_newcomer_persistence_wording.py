"""
Director PART 2, TASK C (wording fix, i18n.py + 3 top100_render.py call
sites that pass the real n= value in - see this file's own report note
on why "i18n.py only" wasn't literally possible without a circular
import).

Three strings wrongly said never-scored newcomers are scored "at the
next nightly run". NEWCOMER_PERSISTENCE_NIGHTS actually holds them
back until they've held a pool slot for that many nights running
(default 3). New wording says that, reading the number from
NEWCOMER_PERSISTENCE_NIGHTS rather than hard-coding it.

Run directly: python3 tests/test_director_taskC_newcomer_persistence_wording.py
"""
import inspect
import sys
from unittest import mock

sys.path.insert(0, ".")

_fail = []
_total = [0]


def check(label, condition):
    _total[0] += 1
    status = "OK" if condition else "FAIL"
    if not condition:
        _fail.append(label)
    print(f"  {status}: {label}")


import i18n
import top100_engine

_KEYS = (
    "top100.changes_strip_awaiting_tooltip",
    "top100.changes_strip_caption",
    "top100.shelf_caption_awaiting",
)

for _key in _KEYS:
    check(f"{_key} (EN): no longer says 'next nightly run'",
          "next nightly run" not in i18n.EN[_key])
    check(f"{_key} (EN): no longer says 'automatically'",
          "automatically" not in i18n.EN[_key])
    check(f"{_key} (EN): uses a {{n}} placeholder (reads the real "
          f"constant, never a hard-coded '3')",
          "{n}" in i18n.EN[_key])
    check(f"{_key} (ES): no longer says 'próxima ejecución nocturna'",
          "próxima ejecución nocturna" not in i18n.ES[_key])
    check(f"{_key} (ES): no longer says 'automáticamente'",
          "automáticamente" not in i18n.ES[_key])
    check(f"{_key} (ES): uses a {{n}} placeholder",
          "{n}" in i18n.ES[_key])

# This test never asserts a specific value for NEWCOMER_PERSISTENCE_
# NIGHTS itself (it changed from 3 to 5 on 16 Oct 2026, Director's
# "wait 3 -> 5 nights" task) - only that the wording is read from
# whatever the live constant is, not hard-coded to either number.
_live_n = top100_engine.NEWCOMER_PERSISTENCE_NIGHTS

check(f"i18n.t() renders the real NEWCOMER_PERSISTENCE_NIGHTS value "
      f"(EN): '... held a pool slot for {_live_n} consecutive nights'",
      f"held a pool slot for {_live_n} consecutive nights"
      in i18n.t("top100.shelf_caption_awaiting", "en", n=_live_n))
check(f"i18n.t() renders the real value (ES): '... {_live_n} noches consecutivas'",
      f"{_live_n} noches consecutivas"
      in i18n.t("top100.shelf_caption_awaiting", "es", n=_live_n))

# Prove it's genuinely READ from the constant, not hard-coded anywhere -
# monkeypatch the constant to a different value and confirm the
# rendered text changes to match, for all three keys.
with mock.patch.object(top100_engine, "NEWCOMER_PERSISTENCE_NIGHTS", 7):
    check("with NEWCOMER_PERSISTENCE_NIGHTS monkeypatched to 7, "
          "shelf_caption_awaiting renders '7' (not hard-coded)",
          "7 consecutive nights"
          in i18n.t("top100.shelf_caption_awaiting", "en",
                     n=top100_engine.NEWCOMER_PERSISTENCE_NIGHTS))
    check("...changes_strip_tooltip renders '7' too",
          "7 consecutive nights"
          in i18n.t("top100.changes_strip_awaiting_tooltip", "en",
                     n=top100_engine.NEWCOMER_PERSISTENCE_NIGHTS))
    check("...changes_strip_caption renders '7' too",
          "7 consecutive nights"
          in i18n.t("top100.changes_strip_caption", "en",
                     n=top100_engine.NEWCOMER_PERSISTENCE_NIGHTS))

# Source-level proof that the one still-live call site in top100_
# render.py (changes_strip_caption, the strip's own footer line) passes
# n=top100_engine.NEWCOMER_PERSISTENCE_NIGHTS - never a literal number -
# so a future change to the constant changes the UI text too, with no
# second place to update. TASK 80 (27 Nov 2026) replaced the bottom
# shelf's own AWAITING caption call site with 3 real reasons - see
# tests/test_director_task80_ai_key_failure_handling.py's own checks on
# shelf_caption_awaiting_newcomer/_queued/_delayed. TASK 81 (28 Nov
# 2026) then removed changes_strip_awaiting_tooltip's own call site too
# (_changes_strip()'s per-chip tooltips are now each ticker's own real
# _shelf_status() caption, never one generic shared tooltip) - see
# tests/test_director_task81_changes_strip_shelf_status.py for that
# behaviour's own tests. changes_strip_awaiting_tooltip's i18n TEXT
# itself (checked above, in _KEYS) is left defined/unused rather than
# deleted - harmless, and this task's own scope is the call sites, not
# a key cleanup.
import top100_render as tr

_src = inspect.getsource(tr)
check("top100_render.py: the one still-live call site (changes_strip_caption) reads "
      "n=top100_engine.NEWCOMER_PERSISTENCE_NIGHTS (never a hard-coded literal)",
      _src.count("n=top100_engine.NEWCOMER_PERSISTENCE_NIGHTS") == 1)


print(f"\nPASS={_total[0] - len(_fail)} FAIL={len(_fail)}")
if _fail:
    sys.exit(1)
