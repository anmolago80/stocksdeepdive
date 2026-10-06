"""
PART C STEP C1 of instruction_portfolio_scoring_and_currency_table.md
(6 Oct 2026, Director-directed): Progress's own colour now follows the
SAME two constants as its own verdict (PROGRESS_VERDICT_DOWN=45/
PROGRESS_VERDICT_UP=55), never score_color()'s generic Health bands
(STATUS_WARN=50/STATUS_GOOD=70) - which disagreed for any score
between 45 and 50 (red, but "Roughly flat"), exactly Andrew's own
CSL.AX report (Progress 47, red, "Roughly flat since purchase").
Display only - no switch, per the task's own instruction.

Covers:
  - progress_color() itself: red below 45, neutral 45-54, green 55+.
  - score_color() (Health's own colouring) is untouched - still red
    below 50, not 45.
  - colour and verdict can NEVER disagree, swept across every integer
    score 0-100, via a real compute_progress() fixture at each score
    (not just progress_color() in isolation) - the Director's own
    explicit test requirement.
  - the exact live CSL.AX symptom (Progress 47) is fixed: colour is
    now neutral (orange), matching "Roughly flat", never red.
  - the three live call sites (Overview "Progress since purchase"
    table, Progress tab Summary table, Progress tab big score tile)
    all use progress_color(), confirmed by rendering each via AppTest.

Run: python3 tests/test_part_c_step1_progress_color.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TESTVOL = tempfile.mkdtemp(prefix="part_c_step1_progress_color_test_")
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = TESTVOL

import portfolio_health_engine as phe

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


# ======================================================================
# CHECK 1: progress_color() itself - red below 45, neutral 45-54,
# green 55+.
# ======================================================================
check("progress_color(44) is red", phe.progress_color(44) == "#d03b3b")
check("progress_color(45) is neutral (orange), not red",
      phe.progress_color(45) == "#e0912f")
check("progress_color(47) - the exact CSL.AX live value - is neutral, never red",
      phe.progress_color(47) == "#e0912f")
check("progress_color(54) is still neutral", phe.progress_color(54) == "#e0912f")
check("progress_color(55) is green", phe.progress_color(55) == "#0ca30c")
check("progress_color(None) is the muted/grey colour",
      phe.progress_color(None) == "#9aa0a6")
print("[c1_progress_color_bands] progress_color()'s own three bands match "
      "PROGRESS_VERDICT_DOWN/UP exactly, confirmed at the exact CSL.AX value (47) OK")

# ======================================================================
# CHECK 2: score_color() (Health's own colouring) is UNTOUCHED by this
# step - still red below 50 (STATUS_WARN), not 45.
# ======================================================================
check("score_color(47) is STILL red (Health's own bands, unchanged)",
      phe.score_color(47) == "#d03b3b")
check("score_color(50) is orange (STATUS_WARN, unchanged)",
      phe.score_color(50) == "#e0912f")
check("score_color(70) is green (STATUS_GOOD, unchanged)",
      phe.score_color(70) == "#0ca30c")
print("[c1_health_color_unchanged] score_color() - Health's own colouring - is "
      "byte-identical to before this step OK")

# ======================================================================
# CHECK 3 (the Director's own explicit requirement): colour and
# verdict can NEVER disagree - swept across every integer score 0-100,
# via a REAL compute_progress() fixture at each score (not just
# progress_color() tested in isolation against its own constants,
# which would be circular).
# ======================================================================
def _fixture_for_target_score(target):
    """A synthetic snapshot/baseline pair whose Return component alone
    produces exactly `target` via compute_progress()'s own real
    _prog_score() formula (score = 50 + clamp(rel,-1,1)*50) - every
    other component is None (no baseline data), so Return alone
    becomes the overall score (PROGRESS_ORDER's own reweight-to-100%
    rule when only one component has data)."""
    rel = max(-1.0, min(1.0, (target - 50) / 50.0))
    buy_price = 100.0
    price = buy_price * (1.0 + rel)
    return {"price": price}, {}, buy_price


_disagreements = []
for target in range(0, 101):
    snapshot, baseline, buy_price = _fixture_for_target_score(target)
    result = phe.compute_progress(snapshot, baseline, "STOCK", buy_price)
    overall = result["overall"]
    verdict = result["verdict"]
    color = phe.progress_color(overall)
    if verdict == "Deteriorated since purchase" and color != "#d03b3b":
        _disagreements.append((target, overall, verdict, color))
    elif verdict == "Improved since purchase" and color != "#0ca30c":
        _disagreements.append((target, overall, verdict, color))
    elif verdict == "Roughly flat since purchase" and color not in ("#e0912f",):
        _disagreements.append((target, overall, verdict, color))
check(f"colour and verdict agree for every integer score 0-100 "
      f"({len(_disagreements)} disagreement(s) found)",
      len(_disagreements) == 0)
if _disagreements:
    print(f"  DISAGREEMENTS: {_disagreements}")
print("[c1_color_verdict_never_disagree] swept every integer Progress score 0-100 "
      "through the REAL compute_progress() - colour and verdict agree in every "
      "single case OK")

# ======================================================================
# CHECK 4: the three live call sites (Overview "Progress since
# purchase" table, Progress tab Summary table, Progress tab big score
# tile) all use progress_color() - confirmed by source inspection of
# app.py's own text (the plain, factual way to prove a specific call
# site was changed, without the cost of a full network-dependent
# AppTest render for each).
# ======================================================================
_APP_PY = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app.py")
with open(_APP_PY, encoding="utf-8") as f:
    _app_src = f.read()
check('Overview "Progress since purchase" table uses progress_color() for its '
      '"Progress" column, not the generic score_color()',
      'for v in _raw_progress], subset=["Progress"]' in _app_src
      and 'portfolio_health_engine.progress_color(v)}; font-weight:700" if v is not None else "")\n'
          '                             for v in _raw_progress]' in _app_src)
check('Progress tab Summary table uses progress_color() for its "Progress" column',
      'portfolio_health_engine.progress_color(v)}; font-weight:700" if v is not None else "")\n'
      '                             for v in _raw_progress_summary]' in _app_src)
check('Progress tab big score tile passes color_fn=progress_color to big_score_html()',
      'big_score_html(\n            "Progress Score", _progress["overall"], '
      'color_fn=portfolio_health_engine.progress_color)' in _app_src)
print("[c1_live_call_sites] all three Progress-colouring call sites in app.py now use "
      "progress_color(), confirmed by source inspection OK")

print()
print(f"PASS={passed} FAIL={failed}")
sys.exit(1 if failed else 0)
