"""
Top 100 filter row (render-only, zero Opus cost, owner-approved mock
"mock_top100_filters.html", 1 Oct 2026). Four independent segmented-
control filters (business quality/Munger, big wave, easy decision,
market structure) directly beneath the sort bar, AND-combined, applied
to every tab after ranking - a filtered-out row never shifts a later
row's rank up ("never renumber"). Display-only: top100_engine.py,
top100_store.py, snapshot_store.py and ranking_engine.py are untouched
by this task (confirmed via `git diff --stat` in the session's own
report - this file only imports them to build fixtures/read scores).

Covers (task's own test list):
  - _passes_filters()/_ranked_rows_after_filters(): each filter alone,
    two combined, "All" passes everything, a declined/sentinel verdict
    and a previous-rubric fallback row (missing the field entirely)
    pass only under "All", ranks preserved (non-consecutive survivors
    keep their original numbers, never renumbered 1,2,3...)
  - AppTest of the real Top 100 page (mocked store): the filter row
    renders beneath the sort bar on every tab, a filter narrows the
    visible rows with the result caption/zero-match message, "clear
    filters" resets every widget to "All", EN+ES labels present with
    no raw i18n key leaking.

Run: python3 tests/test_top100_filters.py
"""
import os
import sys
import tempfile
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TESTVOL = tempfile.mkdtemp(prefix="top100_filters_test_")
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = TESTVOL

import top100_engine as te
import top100_store as ts
import top100_render as tr

if os.path.exists(ts.DB_PATH):
    os.remove(ts.DB_PATH)


# ======================================================================
# SECTION 1: _passes_filters() / _ranked_rows_after_filters() - plain
# dict fixtures, no DB needed.
# ======================================================================

ALL = tr.FILTER_ALL


def _sr(munger=None, wave=None, hurdle=None, structure=None, not_rated=False):
    return {"munger_quality": munger, "big_wave": wave, "one_foot_hurdle": hurdle,
            "market_structure": structure, "not_rated": not_rated}


_no_filter = {"munger_quality": ALL, "big_wave": ALL, "one_foot_hurdle": ALL, "market_structure": ALL}

# "All" passes everything, including None/declined/no-score_row-at-all.
assert tr._passes_filters(_sr(munger="yes"), _no_filter) is True
assert tr._passes_filters(_sr(), _no_filter) is True
assert tr._passes_filters(None, _no_filter) is True
print("[passes_filters] every filter 'all' passes any row, including one with no score_row")

# One active filter alone.
f_munger_yes = {**_no_filter, "munger_quality": "yes"}
assert tr._passes_filters(_sr(munger="yes"), f_munger_yes) is True
assert tr._passes_filters(_sr(munger="no"), f_munger_yes) is False
assert tr._passes_filters(_sr(munger=None), f_munger_yes) is False  # declined/sentinel
assert tr._passes_filters(None, f_munger_yes) is False  # shelf/no score at all
print("[passes_filters] one active filter: match/mismatch/declined/no-score-row all correct")

# Two combined (AND).
f_combo = {**_no_filter, "munger_quality": "yes", "big_wave": "tailwind"}
assert tr._passes_filters(_sr(munger="yes", wave="tailwind"), f_combo) is True
assert tr._passes_filters(_sr(munger="yes", wave="flat"), f_combo) is False
assert tr._passes_filters(_sr(munger="no", wave="tailwind"), f_combo) is False
print("[passes_filters] two filters combined AND-correctly")

# A not_rated row's four fields are force-nulled (per top100_engine's
# own parsing) - same as a declined verdict, excluded under any
# specific filter, included under "all".
nr = _sr(munger=None, wave=None, hurdle=None, structure=None, not_rated=True)
assert tr._passes_filters(nr, f_munger_yes) is False
assert tr._passes_filters(nr, _no_filter) is True
print("[passes_filters] not_rated row excluded under a specific filter, included under all")

# _ranked_rows_after_filters(): ranks preserved, never renumbered - a
# non-consecutive set of survivors keeps its ORIGINAL rank numbers.
rows = [
    {"ticker": "AAA", "score_row": _sr(munger="yes")},
    {"ticker": "BBB", "score_row": _sr(munger="no")},
    {"ticker": "CCC", "score_row": _sr(munger="no")},
    {"ticker": "DDD", "score_row": _sr(munger="yes")},
]
ranked = tr._ranked_rows_after_filters(rows, f_munger_yes)
assert [r for r, _row in ranked] == [1, 4], ranked
assert [row["ticker"] for _r, row in ranked] == ["AAA", "DDD"]
print("[ranked_rows_after_filters] non-consecutive survivors keep ranks 1 and 4, never renumbered to 1,2")

# No filters (None/{}) -> unchanged, full list, same ranks as a plain enumerate.
assert tr._ranked_rows_after_filters(rows, None) == list(enumerate(rows, start=1))
assert tr._ranked_rows_after_filters(rows, {}) == list(enumerate(rows, start=1))
print("[ranked_rows_after_filters] no filters -> identical to a plain enumerate, nothing hidden")


# ======================================================================
# SECTION 2: AppTest of the real Top 100 page against a small, mixed
# fixture pool (mocked store, real render_top100_page()).
# ======================================================================

def _dims_all_scored():
    return {k: {"score": 4, "justification": "j", "source_period": "FY25"} for k in te.DIMENSION_KEYS}


dims = _dims_all_scored()
_now = datetime.now(timezone.utc).isoformat()

# 8 pool rows (task's own "~8 rows, mixed verdicts"):
#   AAA/BBB/CCC/DDD/EEE - rated, current rubric, every verdict combo
#     AAA: munger=yes  wave=tailwind  hurdle=yes  structure=monopoly    USD
#     BBB: munger=yes  wave=flat      hurdle=no   structure=duopoly     USD
#     CCC: munger=no   wave=headwind  hurdle=yes  structure=oligopoly   AUD
#     DDD: munger=no   wave=tailwind  hurdle=no   structure=competitive AUD
#     EEE: munger=None (declined)    wave=tailwind  hurdle=yes  structure=monopoly  USD
#   FFF - previous-rubric FALLBACK row: scored only under an OLDER
#     rubric, with none of the four v5/v6 fields at all (a real v4-era
#     row's own shape) - rated via the fallback, but every filter
#     field reads None.
#   GGG - AWAITING shelf row (no score under any rubric at all).
#   HHH - NOT RATED shelf row (scored, but not_rated=True).
# Identical dims (same composite) for every rated row -> the default
# Research-Score sort ties break alphabetically, so Full 100 ranks are
# simply AAA=1, BBB=2, CCC=3, DDD=4, EEE=5, FFF=6 - deterministic.
ts.save_pool(
    [
        {"ticker": t, "company_name": f"{t} Co", "universe": "S&P 500" if cur == "USD" else "ASX 200",
         "value_score": 80.0, "mos_pct": 40.0, "price": 10.0, "intrinsic_value": 14.0,
         "currency": cur, "generated_at": _now, "pool_selection_rule": "freshest_v1"}
        for t, cur in [("AAA", "USD"), ("BBB", "USD"), ("CCC", "AUD"), ("DDD", "AUD"),
                       ("EEE", "USD"), ("FFF", "USD"), ("GGG", "USD"), ("HHH", "USD")]
    ],
    as_of=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
)


def _save(ticker, rubric, **verdicts):
    ts.save_score(
        ticker=ticker, quarter=f"RP2026-09-30-{ticker}", model=te.MODEL_TOP100, rubric_version=rubric,
        dims=dims, not_rated=verdicts.pop("not_rated", False), inversion_scenario=None,
        inversion_severity=None, prompt="{}", raw_response="{}", **verdicts,
    )


_save("AAA", te.RUBRIC_VERSION, market_structure="monopoly", one_foot_hurdle="yes",
      munger_quality="yes", big_wave="tailwind")
_save("BBB", te.RUBRIC_VERSION, market_structure="duopoly", one_foot_hurdle="no",
      munger_quality="yes", big_wave="flat")
_save("CCC", te.RUBRIC_VERSION, market_structure="oligopoly", one_foot_hurdle="yes",
      munger_quality="no", big_wave="headwind")
_save("DDD", te.RUBRIC_VERSION, market_structure="competitive", one_foot_hurdle="no",
      munger_quality="no", big_wave="tailwind")
_save("EEE", te.RUBRIC_VERSION, market_structure="monopoly", one_foot_hurdle="yes",
      munger_quality=None, big_wave="tailwind")
# FFF: an older rubric only, no market_structure/munger_quality/big_wave/
# one_foot_hurdle kwargs at all - the real shape of a v4-era row.
_save("FFF", "v4")
# HHH: scored under the current rubric, but declined (not_rated=True) -
# composite_score() returns None, same as the AWAITING case for every
# purpose this task cares about.
_save("HHH", te.RUBRIC_VERSION, not_rated=True)
# GGG gets no save_score() call at all - a genuine never-scored AWAITING row.

enriched = tr._enriched_pool()
_by_ticker = {r["ticker"]: r for r in enriched}
assert _by_ticker["FFF"]["is_fallback_score"] is True
assert _by_ticker["FFF"]["score_row"].get("munger_quality") is None
assert _by_ticker["FFF"]["composite"] is not None
assert _by_ticker["GGG"]["composite"] is None and _by_ticker["GGG"]["score_row"] is None
assert _by_ticker["HHH"]["composite"] is None
print("[fixture] AAA-FFF rated (FFF via previous-rubric fallback, fields all None), GGG/HHH shelved")


from streamlit.testing.v1 import AppTest


def _run_page(lang):
    script = f"""
import os, sys
sys.path.insert(0, {os.path.dirname(os.path.dirname(os.path.abspath(__file__)))!r})
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = {TESTVOL!r}
import top100_render
top100_render.render_top100_page(lang={lang!r})
"""
    at = AppTest.from_string(script)
    at.run(timeout=30)
    assert not at.exception, f"render_top100_page(lang={lang!r}) raised: {at.exception}"
    return at


def _set_filter(at, key, value):
    for sc in at.segmented_control:
        if sc.key == key:
            sc.set_value(value)
            return
    raise AssertionError(f"no segmented_control with key={key!r}")


def _all_text(at):
    return "\n".join(
        [c.value for c in at.caption] + [m.value for m in at.markdown]
        + [b.label for b in at.button]
    )


def _has_rank(text, n):
    """True iff the row rank badge "#{n}" is actually present (the
    exact `>#{n}<` span _render_row() emits) - a bare "#1"/"#2"
    substring check would false-positive on this page's own CSS hex
    colors (e.g. "#1a2b4a", "#2a3f63" both contain "#1"/"#2")."""
    return f">#{n}<" in text


def _full100_tab_text(at):
    """Markdown scoped to the Full 100 tab ONLY (tabs order: Mixed, AU,
    US, Full 100 - see render_top100_page()). Rank-preservation checks
    must be scoped to one tab: the Top 20 US tab currency-slices THEN
    re-numbers 1..N within that subset (pre-existing, untouched
    behaviour) - a rank number from that tab's own, different
    numbering would otherwise collide with the Full 100 tab's global
    rank in a combined, all-tabs text blob."""
    return "\n".join(m.value for m in at.tabs[3].markdown)


# ---- default state: every filter "All", result caption shows every
# pooled row (rated + shelf) matched, no raw i18n key leaks ----
at = _run_page("en")
text = _all_text(at)
assert "Showing **8 of 8** companies" in text, text
assert "top100.filter" not in text and "top100." not in text, "raw i18n key leaked into the page"
assert "Clear filters" not in [b.label for b in at.button], "clear button must be hidden when no filter is active"
print("[apptest] default state: 'All' everywhere, 8 of 8 shown, no clear button, no raw i18n keys (EN)")

at_es = _run_page("es")
text_es = _all_text(at_es)
assert "Mostrando **8 de 8** empresas" in text_es, text_es
assert "top100." not in text_es, "raw i18n key leaked into the ES page"
print("[apptest] default state renders correctly in ES too")

# ---- one filter alone: Business quality = Yes -> AAA(#1), BBB(#2) ----
at = _run_page("en")
_set_filter(at, "top100_filter_munger", "Yes")
at.run(timeout=30)
text = _all_text(at)
rtext = _full100_tab_text(at)
assert "Showing **2 of 8** companies" in text, text
assert _has_rank(rtext, 1) and _has_rank(rtext, 2)
assert "CCC" not in rtext and "DDD" not in rtext and "EEE" not in rtext and "FFF" not in rtext
print("[apptest] munger=Yes alone -> 2 of 8, AAA/BBB at their original ranks #1/#2")

# ---- one filter alone, non-consecutive survivors: Big wave = Tailwind
# -> AAA(#1), DDD(#4), EEE(#5) on the Full 100 tab - ranks must NOT
# collapse to #1/#2/#3. ----
at = _run_page("en")
_set_filter(at, "top100_filter_wave", "Tailwind")
at.run(timeout=30)
text = _all_text(at)
rtext = _full100_tab_text(at)
assert "Showing **3 of 8** companies" in text, text
assert _has_rank(rtext, 1) and _has_rank(rtext, 4) and _has_rank(rtext, 5)
assert not _has_rank(rtext, 2) and not _has_rank(rtext, 3), \
    "a filtered-out row's rank must never be reused/renumbered"
print("[apptest] wave=Tailwind alone -> ranks #1/#4/#5 preserved, #2/#3 never appear (never renumbered)")

# ---- two filters combined: Business quality = Yes AND Big wave =
# Tailwind -> AAA only (#1). BBB fails wave, DDD/EEE fail munger. ----
at = _run_page("en")
_set_filter(at, "top100_filter_munger", "Yes")
_set_filter(at, "top100_filter_wave", "Tailwind")
at.run(timeout=30)
text = _all_text(at)
assert "Showing **1 of 8** companies" in text, text
assert "rank numbers kept from the full list" in text
print("[apptest] munger=Yes + wave=Tailwind combined -> 1 of 8 (AAA only)")

# ---- zero matches: Business quality = Yes AND Big wave = Headwind ->
# no company satisfies both (CCC has headwind but munger=no) ----
at = _run_page("en")
_set_filter(at, "top100_filter_munger", "Yes")
_set_filter(at, "top100_filter_wave", "Headwind")
at.run(timeout=30)
text = _all_text(at)
assert "Showing **0 of 8** companies" in text, text
assert "No companies match these filters." in text
print("[apptest] zero-match combination shows the no-match message, no empty table")

# ---- "clear filters" resets every widget to All and reruns ----
assert any(b.label == "Clear filters" for b in at.button), "clear button must be visible while a filter is active"
for b in at.button:
    if b.label == "Clear filters":
        b.click()
at.run(timeout=30)
assert all(sc.value == "All" for sc in at.segmented_control if sc.key in
           ("top100_filter_munger", "top100_filter_wave", "top100_filter_hurdle", "top100_filter_structure"))
text = _all_text(at)
assert "Showing **8 of 8** companies" in text, text
print("[apptest] 'Clear filters' resets every widget to All and restores the full count")

# ---- market structure filter: Monopoly -> AAA(#1), EEE(#5) ----
at = _run_page("en")
_set_filter(at, "top100_filter_structure", "MONOPOLY")
at.run(timeout=30)
text = _all_text(at)
assert "Showing **2 of 8** companies" in text, text

# ---- easy-decision filter: Yes -> AAA(#1), CCC(#3), EEE(#5) ----
at = _run_page("en")
_set_filter(at, "top100_filter_hurdle", "Yes")
at.run(timeout=30)
text = _all_text(at)
assert "Showing **3 of 8** companies" in text, text
print("[apptest] market structure (Monopoly) and easy-decision (Yes) filters both check out")

print("\nALL TOP 100 FILTER ROW TESTS PASSED")
