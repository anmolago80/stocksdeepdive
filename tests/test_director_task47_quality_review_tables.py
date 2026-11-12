"""
Director TASK 47 (12 Nov 2026, "needed for the Director's review
tomorrow morning"): a "good companies being dropped" check in the
admin preview, report only - no change to ranking/selection.

For the final combined option (Quality v2 + Moat blend + Quality
exception), adds a "Drop-outs to review" section: every drop-out
meeting ANY of Moat >= 70 / Research Score >= 70 / Munger-quality =
yes or Market structure in {monopoly, duopoly, oligopoly} / Quality
v1 >= 80, showing ticker/company/sector/Quality v1/Quality v2/Moat/
Research Score/Munger/Market structure/Value Score now vs new setup/
how far below the new cut-off/main component driver - sorted by
Research Score. Also an "Entrants to review" table for new members
with Research Score < 50 or Moat < 30.

Covers:
  - _value_score_component_contributions() reproduces ranking_engine.
    calculate_long_score()'s own real output exactly (plain formula,
    moat_blend with Moat, moat_blend with Moat missing) - proves the
    "main driver" breakdown can never silently drift from the real
    selection formula.
  - _value_score_main_driver() picks the component with the biggest
    contribution swing.
  - _dropout_review_flagged()/_entrant_review_flagged() - each of the
    Director's own named thresholds, independently.
  - top100_engine.quality_review_tables() end to end: which tickers
    land in each table, the exact fields on each row, sort order
    (Research Score descending, unscored last), the no-longer-
    eligible-at-all drop-out case (falls back to the live pool's own
    fields, never crashes), and that neither table mutates sim_result
    or touches the live pool/selection at all.
  - app.py's own render helper never breaks on an empty table (shaped
    for st.table(), no crash on `(none)` placeholder).

ALL FIXTURES IN THIS FILE ARE SYNTHETIC - this sandbox has no outbound
network access, same disclosure as every other fixture-based test in
this repo.

Run: python3 tests/test_director_task47_quality_review_tables.py
"""
import os
import sys
import tempfile
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TESTVOL = tempfile.mkdtemp(prefix="task47_quality_review_test_")
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = TESTVOL

import ranking_engine as re
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


# ======================================================================
# CHECK 1-4: _value_score_component_contributions() reproduces
# calculate_long_score()'s own real output exactly, for every shape
# the main-driver breakdown needs (plain formula, moat_blend with
# Moat, moat_blend with Moat missing).
# ======================================================================
_old = te._value_score_component_contributions(80.0, 69.6, 42.7, moat=None, moat_blend=False)
check("plain-formula breakdown sums to calculate_long_score()'s own real output "
      "(within its own 2dp rounding)",
      abs(sum(_old.values()) - re.calculate_long_score(80.0, 69.6, 42.7, 0, discovery_measured=False)) < 0.01)
check("plain-formula breakdown has no 'moat' key at all - that component doesn't exist in this formula",
      "moat" not in _old)

# calculate_long_score() itself rounds to 2dp (round(..., 2)) while
# the raw component sum doesn't - so the tolerance here must cover
# that rounding, not just float noise (< 1e-6 would be wrong: the
# real gap is up to 0.005).
_new = te._value_score_component_contributions(85.0, 69.6, 42.7, moat=60.0, moat_blend=True)
check("moat_blend breakdown (Moat present) sums to calculate_long_score()'s own real output "
      "(within its own 2dp rounding)",
      abs(sum(_new.values()) - re.calculate_long_score(
          85.0, 69.6, 42.7, 0, mode="moat_blend", moat_score=60.0, discovery_measured=False)) < 0.01)

_new_nomoat = te._value_score_component_contributions(85.0, 69.6, 42.7, moat=None, moat_blend=True)
check("moat_blend breakdown (Moat missing, dropped+reweighted) sums to calculate_long_score()'s "
      "own real output (within its own 2dp rounding)",
      abs(sum(_new_nomoat.values()) - re.calculate_long_score(
          85.0, 69.6, 42.7, 0, mode="moat_blend", moat_score=None, discovery_measured=False)) < 0.01)


# ======================================================================
# CHECK 5-8: _value_score_main_driver() - picks the biggest swing.
# ======================================================================
_row_quality_driven = {"quality": 40.0, "quality_v2": 95.0, "mos_pct": 10.0, "psychology": 0.0, "moat": None}
check("a huge Quality v1->v2 swing (40->95), small/no Moat, is flagged as the Quality driver",
      te._value_score_main_driver(_row_quality_driven) == "Quality")

_row_moat_driven = {"quality": 70.0, "quality_v2": 70.0, "mos_pct": 10.0, "psychology": 0.0, "moat": 95.0}
check("Quality unchanged (v1==v2), a strong new Moat score is flagged as the Moat driver",
      te._value_score_main_driver(_row_moat_driven) == "Moat")

_row_no_change = {"quality": 50.0, "quality_v2": None, "mos_pct": 0.0, "psychology": 0.0, "moat": None}
check("no Quality v2, no Moat - new formula differs from old only via reweighting, "
      "still returns a driver (never raises)", te._value_score_main_driver(_row_no_change) in
      ("Quality", "MOS", "Psychology", "Moat", None))

check("_VALUE_SCORE_COMPONENT_LABELS covers every key _value_score_component_contributions() "
      "can return", set(te._VALUE_SCORE_COMPONENT_LABELS.keys()) == {"quality", "moat", "mos", "psy"})


# ======================================================================
# CHECK 9-16: _dropout_review_flagged() - each criterion independently,
# and the "none of them" case.
# ======================================================================
check("Moat >= 70 alone flags", te._dropout_review_flagged(70.0, None, None, None, None) is True)
check("Moat just under 70 does NOT flag on Moat alone",
      te._dropout_review_flagged(69.9, None, None, None, None) is False)
check("Research Score >= 70 alone flags", te._dropout_review_flagged(None, 70.0, None, None, None) is True)
check("Munger-quality == 'yes' alone flags", te._dropout_review_flagged(None, None, "yes", None, None) is True)
check("Munger-quality == 'no' does NOT flag", te._dropout_review_flagged(None, None, "no", None, None) is False)
check("Market structure 'monopoly' flags", te._dropout_review_flagged(None, None, None, "monopoly", None) is True)
check("Market structure 'competitive' does NOT flag",
      te._dropout_review_flagged(None, None, None, "competitive", None) is False)
check("Quality v1 >= 80 alone flags", te._dropout_review_flagged(None, None, None, None, 80.0) is True)
check("none of the five criteria -> not flagged",
      te._dropout_review_flagged(50.0, 40.0, "no", "competitive", 60.0) is False)


# ======================================================================
# CHECK 17-20: _entrant_review_flagged().
# ======================================================================
check("Research Score < 50 alone flags", te._entrant_review_flagged(None, 49.9) is True)
check("Research Score >= 50 and no Moat info -> not flagged", te._entrant_review_flagged(None, 50.0) is False)
check("Moat < 30 alone flags", te._entrant_review_flagged(29.9, None) is True)
check("neither signal available (both None) -> never flagged ('weak' can't be read off "
      "data that doesn't exist)", te._entrant_review_flagged(None, None) is False)


# ======================================================================
# CHECK 21-30: quality_review_tables() end to end.
# ======================================================================
# Live pool: GOODDROP (strong on every signal, drops out), WEAKDROP
# (weak everywhere, drops out - never flagged), STAYER (stays in both
# pools - never a drop-out or entrant).
ts.save_pool([
    {"ticker": "GOODDROP", "company_name": "Good Drop Co", "sector": "Industrials", "value_score": 65.0,
     "mos_pct": 10.0, "price": 100.0, "intrinsic_value": 110.0, "currency": "USD", "psychology": 0.0},
    {"ticker": "WEAKDROP", "company_name": "Weak Drop Co", "sector": "Industrials", "value_score": 64.0,
     "mos_pct": 5.0, "price": 100.0, "intrinsic_value": 105.0, "currency": "USD", "psychology": 0.0},
    {"ticker": "STAYER", "company_name": "Stayer Co", "sector": "Industrials", "value_score": 80.0,
     "mos_pct": 10.0, "price": 100.0, "intrinsic_value": 115.0, "currency": "USD", "psychology": 0.0},
], as_of="2026-11-11")

_sim_rows = {
    # GOODDROP: Moat 95 (flags), still drops out under the new setup.
    "GOODDROP": {"ticker": "GOODDROP", "company_name": "Good Drop Co", "sector": "Industrials",
                 "quality": 60.0, "quality_v2": 62.0, "moat": 95.0, "mos_pct": 10.0, "psychology": 0.0,
                 "value_score": 65.0, "value_score_v2_moat": 55.0},
    # WEAKDROP: nothing flags it - must NOT appear in dropouts_to_review.
    "WEAKDROP": {"ticker": "WEAKDROP", "company_name": "Weak Drop Co", "sector": "Industrials",
                 "quality": 55.0, "quality_v2": 50.0, "moat": 20.0, "mos_pct": 5.0, "psychology": 0.0,
                 "value_score": 64.0, "value_score_v2_moat": 50.0},
    "STAYER": {"ticker": "STAYER", "company_name": "Stayer Co", "sector": "Industrials",
               "quality": 70.0, "quality_v2": 75.0, "moat": 50.0, "mos_pct": 10.0, "psychology": 0.0,
               "value_score": 80.0, "value_score_v2_moat": 82.0},
    # ENTRANTSTRONG: entrant, high Research Score, high Moat - never flagged.
    "ENTRANTSTRONG": {"ticker": "ENTRANTSTRONG", "company_name": "Entrant Strong Co", "sector": "Healthcare",
                       "quality": 80.0, "quality_v2": 85.0, "moat": 80.0, "mos_pct": 15.0, "psychology": 0.0,
                       "value_score": 60.0, "value_score_v2_moat": 78.0},
    # ENTRANTWEAK: entrant, low Moat AND low Research Score - flagged.
    "ENTRANTWEAK": {"ticker": "ENTRANTWEAK", "company_name": "Entrant Weak Co", "sector": "Healthcare",
                     "quality": 60.0, "quality_v2": 65.0, "moat": 10.0, "mos_pct": 15.0, "psychology": 0.0,
                     "value_score": 58.0, "value_score_v2_moat": 76.0},
}
_sim_result = {
    "eligible_count": 5, "backfill_live": False, "rows": _sim_rows,
    "pool_tickers": {"STAYER", "ENTRANTSTRONG", "ENTRANTWEAK"}, "extension_tickers": set(),
    "cutoff": 76.0,
}


def _score(ticker, research_score_dims_value, munger=None, structure=None, rubric_version=None):
    """Seeds a real top100_scores row via save_score() so composite_
    score()'s own real formula (not a hand-rolled stand-in) produces
    the research_score quality_review_tables() reads."""
    ts.save_score(
        ticker=ticker, quarter="RP2026-09-30", model=te.MODEL_TOP100,
        rubric_version=rubric_version or te.RUBRIC_VERSION,
        dims={k: {"score": research_score_dims_value, "justification": "x", "source_period": "RP2026-09-30"}
              for k in te.DIMENSION_KEYS},
        not_rated=False, most_recent_quarter="2026-09-30",
        inversion_scenario="t", inversion_severity=2, current_headwind=None,
        market_structure=structure, market_structure_comment="x" if structure else None,
        one_foot_hurdle=None, one_foot_comment=None,
        munger_quality=munger, munger_comment="x" if munger else None,
        big_wave=None, big_wave_comment=None,
        prompt="{}", raw_response="{}",
    )


_score("GOODDROP", 5, munger="yes")           # Research Score high (5/5 -> 100) + Munger yes - two flags
_score("WEAKDROP", 2)                          # Research Score moderate, nothing else - must NOT flag
_score("STAYER", 4)
_score("ENTRANTSTRONG", 5)                     # high Research Score
_score("ENTRANTWEAK", 1, structure="competitive")  # low Research Score (1/5 -> 0), Moat 10 - two flags

_review = te.quality_review_tables(_sim_result, log=lambda *a: None)
_dropout_tickers = [r["ticker"] for r in _review["dropouts_to_review"]]
_entrant_tickers = [r["ticker"] for r in _review["entrants_to_review"]]

check("GOODDROP (Moat 95 >= 70, Munger=yes) is in dropouts_to_review", "GOODDROP" in _dropout_tickers)
check("WEAKDROP (nothing flags it) is NOT in dropouts_to_review", "WEAKDROP" not in _dropout_tickers)
check("STAYER (not a drop-out at all - still in both pools) never appears in either table",
      "STAYER" not in _dropout_tickers and "STAYER" not in _entrant_tickers)
check("ENTRANTWEAK (Moat 10 < 30, Research Score low) is in entrants_to_review",
      "ENTRANTWEAK" in _entrant_tickers)
check("ENTRANTSTRONG (strong on both signals) is NOT in entrants_to_review",
      "ENTRANTSTRONG" not in _entrant_tickers)

_gooddrop_row = next(r for r in _review["dropouts_to_review"] if r["ticker"] == "GOODDROP")
check("GOODDROP row carries Quality v1/v2/Moat straight from the sim row",
      _gooddrop_row["quality"] == 60.0 and _gooddrop_row["quality_v2"] == 62.0 and _gooddrop_row["moat"] == 95.0)
check("GOODDROP row's research_score is a real composite_score() figure (5/5 on every "
      "dimension -> 100.0), not hand-rolled", _gooddrop_row["research_score"] == 100.0)
check("GOODDROP row carries munger_quality='yes' and market_structure=None (never set for it)",
      _gooddrop_row["munger_quality"] == "yes" and _gooddrop_row["market_structure"] is None)
check("GOODDROP row's value_score_now is the LIVE value_score (65.0, today's real formula)",
      _gooddrop_row["value_score_now"] == 65.0)
check("GOODDROP row's value_score_new is the new setup's value_score_v2_moat (55.0)",
      _gooddrop_row["value_score_new"] == 55.0)
check("GOODDROP row's below_cutoff is cutoff(76.0) - value_score_new(55.0) = 21.0",
      _gooddrop_row["below_cutoff"] == 21.0)
check("GOODDROP row's main_driver is one of the 4 named components, never raw/None here "
      "(a real, eligible sim row)", _gooddrop_row["main_driver"] in ("Quality", "Moat", "MOS", "Psychology"))

_entrantweak_row = next(r for r in _review["entrants_to_review"] if r["ticker"] == "ENTRANTWEAK")
check("an entrant row has below_cutoff=None (that column is drop-out-only)",
      _entrantweak_row["below_cutoff"] is None)
check("an entrant row has main_driver=None (that column is drop-out-only)",
      _entrantweak_row["main_driver"] is None)
check("ENTRANTWEAK row carries its own market_structure='competitive'",
      _entrantweak_row["market_structure"] == "competitive")


# ======================================================================
# CHECK 31-33: sort order - Research Score descending, unscored last.
# ======================================================================
ts.save_pool([
    {"ticker": "HIGHRS", "company_name": "High RS Co", "sector": "Industrials", "value_score": 65.0,
     "mos_pct": 10.0, "price": 100.0, "intrinsic_value": 110.0, "currency": "USD", "psychology": 0.0},
    {"ticker": "LOWRS", "company_name": "Low RS Co", "sector": "Industrials", "value_score": 65.0,
     "mos_pct": 10.0, "price": 100.0, "intrinsic_value": 110.0, "currency": "USD", "psychology": 0.0},
    {"ticker": "UNSCOREDDROP", "company_name": "Unscored Drop Co", "sector": "Industrials", "value_score": 65.0,
     "mos_pct": 10.0, "price": 100.0, "intrinsic_value": 110.0, "currency": "USD", "psychology": 0.0},
], as_of="2026-11-12")
_sort_rows = {
    t: {"ticker": t, "company_name": f"{t} Co", "sector": "Industrials", "quality": 85.0, "quality_v2": 85.0,
        "moat": 90.0, "mos_pct": 10.0, "psychology": 0.0, "value_score": 65.0, "value_score_v2_moat": 50.0}
    for t in ("HIGHRS", "LOWRS", "UNSCOREDDROP")
}
_sort_sim = {"rows": _sort_rows, "pool_tickers": set(), "extension_tickers": set(), "cutoff": 76.0}
_score("HIGHRS", 5)
_score("LOWRS", 3)
# UNSCOREDDROP deliberately never scored - still flagged (Moat 90 >= 70), must sort LAST.

_sort_review = te.quality_review_tables(_sort_sim, log=lambda *a: None)
_sort_tickers = [r["ticker"] for r in _sort_review["dropouts_to_review"]]
check("all 3 are flagged (Moat 90 >= 70) and present", set(_sort_tickers) == {"HIGHRS", "LOWRS", "UNSCOREDDROP"})
check("sorted by Research Score descending (HIGHRS before LOWRS)",
      _sort_tickers.index("HIGHRS") < _sort_tickers.index("LOWRS"))
check("the unscored one (UNSCOREDDROP) sorts LAST, never treated as a zero ahead of a low "
      "real score", _sort_tickers[-1] == "UNSCOREDDROP")


# ======================================================================
# CHECK 34-36: a drop-out no longer eligible at all under the new
# setup (absent from sim_result["rows"] entirely) - falls back to the
# live pool's own fields for company_name/sector/value_score_now,
# never crashes, Quality v2/Moat/value_score_new/below_cutoff/
# main_driver all read None.
# ======================================================================
ts.save_pool([
    {"ticker": "INELIGIBLE", "company_name": "Ineligible Co", "sector": "Energy", "value_score": 70.0,
     "mos_pct": 10.0, "price": 100.0, "intrinsic_value": 110.0, "currency": "USD", "psychology": 0.0},
], as_of="2026-11-13")
_score("INELIGIBLE", 5)  # Research Score 100 -> flagged even with no sim row at all
_ineligible_sim = {"rows": {}, "pool_tickers": set(), "extension_tickers": set(), "cutoff": 76.0}
_ineligible_review = te.quality_review_tables(_ineligible_sim, log=lambda *a: None)
_ineligible_row = next(r for r in _ineligible_review["dropouts_to_review"] if r["ticker"] == "INELIGIBLE")
check("a drop-out absent from sim_result['rows'] entirely still gets a row, never crashes",
      _ineligible_row is not None)
check("its company_name/sector/value_score_now fall back to the LIVE pool's own stored fields",
      _ineligible_row["company_name"] == "Ineligible Co" and _ineligible_row["sector"] == "Energy"
      and _ineligible_row["value_score_now"] == 70.0)
check("its Quality v2/Moat/value_score_new/below_cutoff/main_driver all read None - no "
      "'new setup' row exists to read them from",
      _ineligible_row["quality_v2"] is None and _ineligible_row["moat"] is None
      and _ineligible_row["value_score_new"] is None and _ineligible_row["below_cutoff"] is None
      and _ineligible_row["main_driver"] is None)


# ======================================================================
# CHECK 37-39: nothing here writes anything or mutates its own inputs.
# ======================================================================
_sim_rows_copy = {t: dict(r) for t, r in _sim_rows.items()}
te.quality_review_tables(_sim_result, log=lambda *a: None)
check("quality_review_tables() never mutates sim_result['rows'] in place",
      _sim_rows == _sim_rows_copy)
check("the live pool is untouched by this call (still exactly the one row this test's own "
      "latest save_pool() call wrote - each save_pool() call defines a whole new latest "
      "snapshot, it doesn't merge with an earlier one)",
      {r["ticker"] for r in ts.current_pool()} == {"INELIGIBLE"})

# A lookup failure for one ticker never aborts the whole table.
with mock.patch.object(ts, "latest_score_for_ticker", side_effect=RuntimeError("boom")):
    _err_review = te.quality_review_tables(_sim_result, log=lambda *a: None)
check("a score lookup failure for every ticker is caught per-ticker, never raised - "
      "still returns both tables (possibly smaller, never crashes)",
      isinstance(_err_review, dict) and "dropouts_to_review" in _err_review
      and "entrants_to_review" in _err_review)


# ======================================================================
# CHECK 40: app.py's own render-shaping helper never crashes on an
# empty table, and shapes the exact columns the Director asked for.
# ======================================================================
import app  # a plain Python import works fine outside a real Streamlit runtime -
            # same pattern tests/test_audit_a3_origin_and_auth_hardening.py already uses
_shape_fn = app._quality_review_table_rows
check("_quality_review_table_rows([], is_dropout=True) returns [] (caller supplies the "
      "'(none)' placeholder) without raising", _shape_fn([], is_dropout=True) == [])
_shaped = _shape_fn([_gooddrop_row], is_dropout=True)
check("a dropout row is shaped with 'Below new cut-off'/'Main driver' columns",
      "Below new cut-off" in _shaped[0] and "Main driver" in _shaped[0])
_shaped_entrant = _shape_fn([_entrantweak_row], is_dropout=False)
check("an entrant row is NOT shaped with 'Below new cut-off'/'Main driver' columns "
      "(drop-out-only per the Director's own table spec)",
      "Below new cut-off" not in _shaped_entrant[0] and "Main driver" not in _shaped_entrant[0])


print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
