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

check("NEWCOMER_PERSISTENCE_NIGHTS is still 3 (this fix is wording "
      "only, never a behavior change)",
      top100_engine.NEWCOMER_PERSISTENCE_NIGHTS == 3)

check("i18n.t() renders the real NEWCOMER_PERSISTENCE_NIGHTS value "
      "(EN): '... held a pool slot for 3 consecutive nights'",
      "held a pool slot for 3 consecutive nights"
      in i18n.t("top100.shelf_caption_awaiting", "en",
                 n=top100_engine.NEWCOMER_PERSISTENCE_NIGHTS))
check("i18n.t() renders the real value (ES): '... 3 noches consecutivas'",
      "3 noches consecutivas"
      in i18n.t("top100.shelf_caption_awaiting", "es",
                 n=top100_engine.NEWCOMER_PERSISTENCE_NIGHTS))

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

# Source-level proof that all three real call sites in top100_render.py
# pass n=top100_engine.NEWCOMER_PERSISTENCE_NIGHTS - never a literal
# number - so a future change to the constant changes the UI text too,
# with no second place to update.
import top100_render as tr

_src = inspect.getsource(tr)
check("top100_render.py: all 3 call sites read n=top100_engine."
      "NEWCOMER_PERSISTENCE_NIGHTS (never a hard-coded literal)",
      _src.count("n=top100_engine.NEWCOMER_PERSISTENCE_NIGHTS") == 3)


print(f"\nPASS={_total[0] - len(_fail)} FAIL={len(_fail)}")
if _fail:
    sys.exit(1)
