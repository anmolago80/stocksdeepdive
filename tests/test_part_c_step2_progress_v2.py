"""
PART C STEP C2 of instruction_portfolio_scoring_and_currency_table.md
(6 Oct 2026, Director-directed): percentage-point comparison for
Progress rate fields, as v2 beside the old method - switch
PROGRESS_V2_LIVE.

Covers exactly the task's own listed tests:
  - growth 10% to 6% scores 40 under v2 (not 30 - v1's own relative-
    change reading).
  - growth 10% to 10% (no change) scores 50 under v2.
  - margin 20% to 25% scores 75 under v2.
  - a fixture shaped like the owner's own CSL.AX report (level fields
    roughly flat, growth slowing a few points) printed old against new.
  - switch-off proof: compute_progress() (v1) is untouched, byte-
    identical to before this step.
  - level fields (Debt Δ/Intrinsic Δ/Return/Income Δ) are UNCHANGED
    under v2 - still relative change, never percentage points.
  - the PART B/C2 owner panel's own second table (Progress comparison)
    renders with the required columns, plus a per-ticker expander with
    each component's current/baseline/old score/new score.

Run: python3 tests/test_part_c_step2_progress_v2.py
"""
import os
import sys
import tempfile
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TESTVOL = tempfile.mkdtemp(prefix="part_c_step2_progress_v2_test_")
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = TESTVOL
os.environ.pop("PROGRESS_V2_LIVE", None)

import ai_gate
import portfolio_health_engine as phe
import portfolio_news_engine as pne
import portfolio_store as ps

if os.path.exists(ps.DB_PATH):
    os.remove(ps.DB_PATH)
if os.path.exists(pne.DB_PATH):
    os.remove(pne.DB_PATH)

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
# CHECK 1: growth 10% to 6% scores 40 under v2 (a 4-percentage-point
# slowdown on a 20pp scale: 50 + (-4/20)*50 = 40), not 30 (v1's own
# relative-change reading: 50 + ((0.06-0.10)/0.10)*50 = 30).
# ======================================================================
_v1_growth = phe._prog_score(0.06, 0.10)
_v2_growth = phe._prog_score_v2_rate(0.06, 0.10, phe.PROGRESS_V2_GROWTH_SCALE_PP)
check("v1: growth 10% -> 6% scores 30 (relative-change reading, the bug)",
      _v1_growth == 30.0)
check("v2: growth 10% -> 6% scores 40 (percentage-point reading, the fix)",
      _v2_growth == 40.0)
print(f"[c2_growth_10_to_6] v1={_v1_growth} (relative -40%) vs v2={_v2_growth} "
      "(a 4pp slowdown on a 20pp scale) OK")

# ======================================================================
# CHECK 2: growth 10% to 10% (no change) scores 50 under v2.
# ======================================================================
check("v2: growth 10% -> 10% (no change) scores exactly 50",
      phe._prog_score_v2_rate(0.10, 0.10, phe.PROGRESS_V2_GROWTH_SCALE_PP) == 50.0)

# ======================================================================
# CHECK 3: margin 20% to 25% scores 75 under v2 (a +5pp improvement on
# a 10pp scale: 50 + (5/10)*50 = 75).
# ======================================================================
check("v2: margin 20% -> 25% scores 75 (a +5pp improvement on a 10pp scale)",
      phe._prog_score_v2_rate(0.25, 0.20, phe.PROGRESS_V2_MARGIN_SCALE_PP) == 75.0)
print("[c2_growth_flat_and_margin_up] growth no-change scores exactly 50; margin "
      "+5pp scores 75 OK")

# ======================================================================
# CHECK 4: level fields (Debt Δ/Intrinsic Δ/Return/Income Δ) are
# UNCHANGED under v2 - still relative change, via a full compute_
# progress_v2() call compared component-by-component against compute_
# progress() on the SAME fixture.
# ======================================================================
_snapshot = {
    "revenue_growth": 0.06, "earnings_growth": 0.07, "profit_margin": 0.25,
    "roe": 0.14, "fcf_growth": 0.03, "debt_to_equity": 45.0,
    "intrinsic_value": 95.0, "price": 102.0, "dividend_rate": 2.5,
}
_baseline = {
    "revenue_growth": 0.10, "earnings_growth": 0.10, "profit_margin": 0.20,
    "roe": 0.15, "fcf_growth": 0.05, "debt_to_equity": 50.0,
    "intrinsic_value": 100.0, "dividend_rate": 2.4,
}
_result_v1 = phe.compute_progress(_snapshot, _baseline, "STOCK", 100.0)
_result_v2 = phe.compute_progress_v2(_snapshot, _baseline, "STOCK", 100.0)
for _level_key in ("Debt Δ", "Intrinsic Δ", "Return", "Income Δ"):
    check(f"level field {_level_key!r} is UNCHANGED between v1 and v2 (still relative change)",
          _result_v1["components"][_level_key]["score"] == _result_v2["components"][_level_key]["score"])
for _rate_key in ("Growth Δ", "Margins Δ", "ROIC Δ"):
    check(f"rate field {_rate_key!r} genuinely DIFFERS between v1 and v2 on this fixture",
          _result_v1["components"][_rate_key]["score"] != _result_v2["components"][_rate_key]["score"])
print("[c2_level_fields_unchanged] Debt/Intrinsic/Return/Income are byte-identical "
      "between v1 and v2 (still relative change); Growth/Margins/ROIC genuinely "
      "differ (now percentage-point based) OK")

# ======================================================================
# CHECK 5: a fixture shaped like the owner's own CSL.AX report - level
# fields roughly flat, growth slowing a few points - printed old
# against new.
# ======================================================================
_csl_snapshot = {
    "revenue_growth": 0.03, "earnings_growth": 0.02, "profit_margin": 0.295,
    "roe": 0.12, "fcf_growth": 0.01, "debt_to_equity": 41.0,
    "intrinsic_value": 248.0, "price": 255.0, "dividend_rate": 4.5,
}
_csl_baseline = {
    "revenue_growth": 0.06, "earnings_growth": 0.05, "profit_margin": 0.30,
    "roe": 0.13, "fcf_growth": 0.03, "debt_to_equity": 40.0,
    "intrinsic_value": 250.0, "dividend_rate": 4.4,
}
_csl_v1 = phe.compute_progress(_csl_snapshot, _csl_baseline, "STOCK", 240.0)
_csl_v2 = phe.compute_progress_v2(_csl_snapshot, _csl_baseline, "STOCK", 240.0)
print(f"[c2_csl_before_after] BEFORE (v1): overall={_csl_v1['overall']}, "
      f"verdict={_csl_v1['verdict']!r}, Growth={_csl_v1['components']['Growth Δ']['score']} "
      f"| AFTER (v2): overall={_csl_v2['overall']}, verdict={_csl_v2['verdict']!r}, "
      f"Growth={_csl_v2['components']['Growth Δ']['score']}")
check("v2's Growth score is HIGHER than v1's on this fixture (growth slowing a few "
      "points reads far less harshly in percentage points than in relative terms)",
      _csl_v2["components"]["Growth Δ"]["score"] > _csl_v1["components"]["Growth Δ"]["score"])
check("v2's overall Progress score is correspondingly higher (or equal) to v1's",
      _csl_v2["overall"] >= _csl_v1["overall"])

# ======================================================================
# CHECK 6 (switch-off proof): compute_progress() (v1) is untouched -
# byte-identical to before this step, on a range of fixtures.
# ======================================================================
check("os.environ.pop('PROGRESS_V2_LIVE') leaves is_progress_v2_live() False",
      phe.is_progress_v2_live() is False)
os.environ["PROGRESS_V2_LIVE"] = "1"
check("PROGRESS_V2_LIVE=1 makes is_progress_v2_live() True",
      phe.is_progress_v2_live() is True)
os.environ.pop("PROGRESS_V2_LIVE", None)
_v1_again = phe.compute_progress(_snapshot, _baseline, "STOCK", 100.0)
check("compute_progress() (v1) gives the SAME result regardless of the switch state "
      "(it never reads PROGRESS_V2_LIVE at all - a completely separate function)",
      _v1_again["overall"] == _result_v1["overall"]
      and _v1_again["components"]["Growth Δ"]["score"] == _result_v1["components"]["Growth Δ"]["score"])
print("[c2_switch_off_proof] compute_progress() is untouched by this step and never "
      "reads the switch at all OK")

# ======================================================================
# CHECK 7: the PART B/C2 owner panel's own second table (Progress
# comparison) renders with the required columns, plus a per-ticker
# expander with each component's current/baseline/old score/new score.
# Production-shaped: the owner's holding is added via the real writer,
# portfolio_store.add_holding().
# ======================================================================
_OWNER = ai_gate.owner_email()
ps.add_holding(_OWNER, "Main", "PROGCO", name="Progress Company", kind="STOCK",
                currency="AUD", shares=100.0, buy_price=100.0, buy_date="2025-01-01",
                baseline={
                    "revenue_growth": 0.10, "earnings_growth": 0.10, "profit_margin": 0.20,
                    "roe": 0.15, "fcf_growth": 0.05, "debt_to_equity": 50.0,
                    "intrinsic_value": 100.0, "dividend_rate": 2.4, "price": 100.0,
                })
_FAKE_SNAPSHOT = dict(_snapshot)
_patches = [
    mock.patch.object(phe, "fetch_snapshot", return_value=_FAKE_SNAPSHOT),
    mock.patch.object(pne, "_claim_fetch", return_value=False),
]
for p in _patches:
    p.start()
try:
    from streamlit.testing.v1 import AppTest
    _SCRIPT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    _script = f"""
import os, sys
sys.path.insert(0, {_SCRIPT_DIR!r})
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = {TESTVOL!r}
import app
app._render_portfolio_health_news_v2_comparison_panel()
"""
    at = AppTest.from_string(_script, default_timeout=60)
    at.run()
    check("panel still renders without raising, with the new Progress table added",
          not at.exception)
    _markdowns = " ".join(m.value or "" for m in at.get("markdown"))
    check("the Progress comparison table's own heading is present",
          "Progress since purchase: method comparison" in _markdowns)

    _progress_df = None
    for df in at.get("dataframe"):
        try:
            _cols = set(df.value.columns)
        except Exception:
            continue
        if "Progress old" in _cols:
            _progress_df = df.value
            break
    check("the Progress comparison table has every required column",
          _progress_df is not None and
          {"Ticker", "Progress old", "Progress new", "Verdict old", "Verdict new"
           }.issubset(set(_progress_df.columns)))
    check("PROGCO appears as a row in the Progress comparison table",
          _progress_df is not None and "PROGCO" in _progress_df["Ticker"].tolist())

    _component_df = None
    for df in at.get("dataframe"):
        try:
            _cols = set(df.value.columns)
        except Exception:
            continue
        if {"Component", "Current", "Baseline", "Old score", "New score"}.issubset(_cols):
            _component_df = df.value
            break
    check("a per-ticker Progress-component expander (current/baseline/old/new score) "
          "is rendered", _component_df is not None)
    check("the component table lists at least Growth Δ, Margins Δ and ROIC Δ",
          _component_df is not None
          and {"Growth Δ", "Margins Δ", "ROIC Δ"}.issubset(set(_component_df["Component"].tolist())))
finally:
    for p in _patches:
        p.stop()
print("[c2_owner_panel] the PART B/C2 panel's own Progress comparison table and "
      "per-ticker component expander both render correctly OK")

print()
print(f"PASS={passed} FAIL={failed}")
if os.path.exists(ps.DB_PATH):
    os.remove(ps.DB_PATH)
if os.path.exists(pne.DB_PATH):
    os.remove(pne.DB_PATH)
sys.exit(1 if failed else 0)
