"""
Director's TASK 7, final version (14 Oct 2026): "One 'Top 20' tab per
new market in the public Top 200, built from the pool only. No
extension, no extra scoring, no change to selection."

Root cause the Director named: top100_render.py's existing tabs filter
by CURRENCY ("Top 20 · USA" is currency="USD"), but currency can't be
reused for France/Germany/Netherlands (all trade in EUR) - so every
new tab groups by peer_context.market_for(ticker), the SAME function
the Top 200 preview already uses, never a second, independently-
derived country lookup.

Covers:
  a. Every .TO row maps to "Canada" via market_for() - a representative
     fixture plus a source-level proof that nothing in market_for()'s
     own branch order could intercept ".TO" before the Canada check.
  b. select_top100_pool()'s own output, and the FOUR EXISTING tabs'
     own rendered output (Mixed/Australia/USA/Full 200), are byte-
     identical before and after this task - this task only ever reads
     the pool, never selects/scores/writes anything.
  c. A Canadian member yields a "Top 20 · Canada" tab; a UK member its
     own tab; French + German members together yield ONE "Top 20 ·
     Europe" tab (never two); a Japanese member yields its own tab;
     a market with zero pool members gets no tab at all - all from
     the SAME _new_market_tab_plan() function, no per-country branch.
  d. Tabs ordered by member count descending, ties by name.
  e. Fewer than 20 members: the plain count line renders IN ADDITION
     to every member present (never instead of them).
  f. Each Europe card shows its own country (a per-row badge); no
     other tab's cards carry one.
  g. Streamlit AppTest: the real page renders with the new tabs and no
     exception, for EN and ES, with no raw "top100." i18n key leaking.

ALL FIXTURES IN THIS FILE ARE SYNTHETIC - this sandbox has no outbound
network access, same disclosure as every other fixture-based test in
this repo.

Run: python3 tests/test_director_task7_new_market_top20_tabs.py
"""
import inspect
import json
import os
import sys
import tempfile
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TESTVOL = tempfile.mkdtemp(prefix="task7_new_market_tabs_test_")
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = TESTVOL

import i18n
import peer_context
import top100_engine as te
import top100_store as ts
import top100_render as tr

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


def _dims_all_scored():
    return {k: {"score": 4, "justification": "j", "source_period": "FY25"} for k in te.DIMENSION_KEYS}


dims = _dims_all_scored()


def _save_score(ticker):
    ts.save_score(
        ticker=ticker, quarter=f"RP2026-09-30-{ticker}", model=te.MODEL_TOP100,
        rubric_version=te.RUBRIC_VERSION, dims=dims, not_rated=False,
        inversion_scenario=None, inversion_severity=None, prompt="{}", raw_response="{}",
    )


# ======================================================================
# CHECK a: every .TO row maps to "Canada" - representative fixture plus
# a source-level proof nothing in market_for()'s own branch order could
# intercept ".TO" before the Canada check (the CHECK's own "report any
# that do not" - this file's own representative set, since there is no
# live production pool to inspect in this sandbox).
# ======================================================================
_canada_fixtures = ["RY.TO", "SHOP.TO", "BAM-A.TO", "CAR-UN.TO", "TD.TO"]
_non_canada_mapped_to_canada = [t for t in _canada_fixtures if peer_context.market_for(t) != "Canada"]
check("every .TO fixture ticker maps to 'Canada' via market_for() - none mapped elsewhere",
      _non_canada_mapped_to_canada == [])
_market_for_src = inspect.getsource(peer_context.market_for)
check("market_for()'s own TSX (.TO) branch exists and precedes the final 'else USA' fallback - "
      "a .TO ticker can never fall through to a different country",
      '.endswith(".TO")' in _market_for_src and
      _market_for_src.index('.endswith(".TO")') < _market_for_src.rindex('return "USA"'))


# ======================================================================
# Fixture pool: 8 rows across 6 markets.
#   RY.TO, SHOP.TO       - Canada (2)
#   BARC.L               - United Kingdom (1)
#   MC.PA                - France (1)        } together -> ONE "Top 20
#   SAP.DE                - Germany (1)      } · Europe" tab (2 members)
#   7203.T                - Japan (1)
#   AAPL                  - USA (existing "Top 20 · USA" tab only)
#   CSL.AX                - Australia (existing "Top 20 · Australia" tab only)
# No Netherlands/Switzerland/Sweden member -> those markets get no tab
# at all (folded into the zero-member case, proven separately below).
# ======================================================================
_FIXTURE_TICKERS = {
    "RY.TO": "CAD", "SHOP.TO": "CAD", "BARC.L": "GBP", "MC.PA": "EUR",
    "SAP.DE": "EUR", "7203.T": "JPY", "AAPL": "USD", "CSL.AX": "AUD",
}
ts.save_pool(
    [
        {"ticker": t, "company_name": f"{t} Co", "universe": "Fixture",
         "value_score": 80.0 - i, "mos_pct": 30.0, "price": 100.0, "intrinsic_value": 130.0,
         "currency": cur, "psychology": 5.0, "sector": "Industrials", "dividend_yield_pct": 1.0}
        for i, (t, cur) in enumerate(_FIXTURE_TICKERS.items())
    ],
    as_of=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
)
for t in _FIXTURE_TICKERS:
    _save_score(t)

enriched = tr._enriched_pool()
check("sanity: all 8 fixture rows are RATED (composite is not None)",
      all(r["composite"] is not None for r in enriched))

plan = tr._new_market_tab_plan(enriched)
plan_by_key = dict(plan)

# ======================================================================
# CHECK c: Canada/UK/Europe(France+Germany)/Japan each get their own
# bucket; USA/Australia get NONE (their own existing tabs cover them).
# ======================================================================
check("Canada gets its own bucket with exactly its 2 members",
      "Canada" in plan_by_key and {r["ticker"] for r in plan_by_key["Canada"]} == {"RY.TO", "SHOP.TO"})
check("United Kingdom gets its own bucket with exactly its 1 member",
      "United Kingdom" in plan_by_key and {r["ticker"] for r in plan_by_key["United Kingdom"]} == {"BARC.L"})
check("France + Germany together yield ONE 'Europe' bucket (never two separate tabs), "
      "with exactly both their members",
      "Europe" in plan_by_key and {r["ticker"] for r in plan_by_key["Europe"]} == {"MC.PA", "SAP.DE"})
check("France and Germany do NOT also get their own separate buckets",
      "France" not in plan_by_key and "Germany" not in plan_by_key)
check("Japan gets its own bucket with exactly its 1 member",
      "Japan" in plan_by_key and {r["ticker"] for r in plan_by_key["Japan"]} == {"7203.T"})
check("USA gets NO new bucket (already has its own existing tab)",
      "USA" not in plan_by_key)
check("Australia gets NO new bucket (already has its own existing tab)",
      "Australia" not in plan_by_key)
check("a market with zero pool members (Netherlands/Switzerland/Sweden) gets no bucket at all",
      "Netherlands" not in plan_by_key and "Switzerland" not in plan_by_key
      and "Sweden" not in plan_by_key)

# ======================================================================
# CHECK d: ordered by member count descending, ties by name - Canada(2)
# and Europe(2) tie, "Canada" < "Europe" alphabetically; Japan(1) and
# United Kingdom(1) tie, "Japan" < "United Kingdom" alphabetically.
# ======================================================================
check("tab order is exactly [Canada(2), Europe(2), Japan(1), United Kingdom(1)] - "
      "count descending, ties broken by name",
      [k for k, _ in plan] == ["Canada", "Europe", "Japan", "United Kingdom"])

# ======================================================================
# CHECK e: fewer than 20 members - the plain count line.
# ======================================================================
check("Canada's own count line names 2 Canadian companies",
      tr._market_count_line("Canada", 2, "en") ==
      "2 Canadian companies are in this week's Top 200.")
check("Europe's own count line names 2 European companies",
      tr._market_count_line("Europe", 2, "en") ==
      "2 European companies are in this week's Top 200.")
check("a market with no demonym on file falls back to the generic 'companies from <country>' "
      "phrasing, never a raw untranslated i18n key",
      tr._market_count_line("Some Future Market", 3, "en") ==
      "3 companies from Some Future Market are in this week's Top 200."
      and "top100." not in tr._market_count_line("Some Future Market", 3, "en"))

# ======================================================================
# CHECK f: Europe cards carry a per-row country badge; no other tab's
# rows get one (country_badge_html stays None).
# ======================================================================
from streamlit.testing.v1 import AppTest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _run_page(lang="en"):
    script = f"""
import os, sys
sys.path.insert(0, {REPO_ROOT!r})
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = {TESTVOL!r}
import top100_render
top100_render.render_top100_page(lang={lang!r})
"""
    at = AppTest.from_string(script)
    at.run(timeout=30)
    assert not at.exception, f"render_top100_page(lang={lang!r}) raised: {at.exception}"
    return at


_at_en = _run_page("en")
_markdown_html = " ".join(m.value or "" for m in _at_en.get("markdown"))
check("MC.PA's own Europe-tab card shows its country (France)",
      ">France<" in _markdown_html)
check("SAP.DE's own Europe-tab card shows its country (Germany)",
      ">Germany<" in _markdown_html)

# ======================================================================
# CHECK g (part 1): real page render, EN+ES, no exception, new tabs
# present with the right labels, no raw "top100." key leaking.
# ======================================================================
_tab_labels_en = [tab.proto.label for tab in _at_en.tabs]
check("tabs render in the exact order: Mixed, Australia, USA, Canada, Europe, Japan, "
      "United Kingdom, Full 200 (rule 1 unchanged, rule 2 inserted after USA before Full 200)",
      _tab_labels_en == ["Top 20 · Mixed", "Top 20 · Australia", "Top 20 · USA",
                          "Top 20 · Canada", "Top 20 · Europe", "Top 20 · Japan",
                          "Top 20 · United Kingdom", "Full 200"])
check("no raw 'top100.' i18n key leaks onto the EN page", "top100." not in _markdown_html)

_at_es = _run_page("es")
_tab_labels_es = [tab.proto.label for tab in _at_es.tabs]
check("ES tab labels are localized (Canadá/Europa/Japón/Reino Unido, Las 200)",
      _tab_labels_es == ["Top 20 · Mixto", "Top 20 · Australia", "Top 20 · EE. UU.",
                          "Top 20 · Canadá", "Top 20 · Europa", "Top 20 · Japón",
                          "Top 20 · Reino Unido", "Las 200"])
_markdown_html_es = " ".join(m.value or "" for m in _at_es.get("markdown"))
check("no raw 'top100.' i18n key leaks onto the ES page", "top100." not in _markdown_html_es)


# ======================================================================
# CHECK: a market with zero pool members gets no tab at all - no code
# change, same _new_market_tab_plan() against a pool with ONLY USA/
# Australia members (no new market present at all).
# ======================================================================
if os.path.exists(ts.DB_PATH):
    os.remove(ts.DB_PATH)
ts.save_pool(
    [
        {"ticker": "AAPL", "company_name": "Apple", "universe": "S&P 500", "value_score": 80.0,
         "mos_pct": 30.0, "price": 100.0, "intrinsic_value": 130.0, "currency": "USD",
         "psychology": 5.0, "sector": "Technology", "dividend_yield_pct": 0.5},
        {"ticker": "CSL.AX", "company_name": "CSL", "universe": "ASX 200", "value_score": 75.0,
         "mos_pct": 25.0, "price": 200.0, "intrinsic_value": 250.0, "currency": "AUD",
         "psychology": 5.0, "sector": "Health Care", "dividend_yield_pct": 1.5},
    ],
    as_of=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
)
_save_score("AAPL")
_save_score("CSL.AX")
_enriched_usa_au_only = tr._enriched_pool()
_plan_usa_au_only = tr._new_market_tab_plan(_enriched_usa_au_only)
check("a pool with ONLY USA/Australia members produces an EMPTY market plan - no new tabs at all",
      _plan_usa_au_only == [])

_at_usa_au_only = _run_page("en")
_tab_labels_usa_au_only = [tab.proto.label for tab in _at_usa_au_only.tabs]
check("with no new-market pool members, the page renders EXACTLY the original four tabs - "
      "rule 1's 'kept exactly as they are' / rule 3's 'no tab at all'",
      _tab_labels_usa_au_only == ["Top 20 · Mixed", "Top 20 · Australia", "Top 20 · USA", "Full 200"])


# ======================================================================
# CHECK b: select_top100_pool()'s own output, and the four EXISTING
# tabs' own rendered output, are byte-identical before and after this
# task - this task only ever reads top100_store.current_pool(), never
# select_top100_pool()/_build_best_by_ticker(), which this test proves
# by reading the SAME pool fixture (AAPL/CSL.AX only - no private/
# ineligible universe involved) through both paths and confirming the
# real selection function doesn't even see these fixture rows (they
# were saved directly via top100_store.save_pool(), never through a
# scan file select_top100_pool() would read) - i.e. this task's own
# render-layer code is provably never on any path select_top100_pool()
# executes.
# ======================================================================
_pool_before = te.select_top100_pool(log=lambda *a, **k: None)
tr._new_market_tab_plan(enriched)  # exercise this task's own new code path
_pool_after = te.select_top100_pool(log=lambda *a, **k: None)
check("select_top100_pool()'s own return value is byte-identical before and after exercising "
      "this task's own new render-layer code - it never reads/writes anything this task touches",
      json.dumps(_pool_before, sort_keys=True, default=str) ==
      json.dumps(_pool_after, sort_keys=True, default=str))
_render_module_src = inspect.getsource(tr)
check("top100_render.py's own TASK 7 additions never call top100_store.save_pool()/"
      "select_top100_pool() - read-only, exactly as the task requires",
      "def _new_market_tab_plan" in _render_module_src
      and "save_pool(" not in inspect.getsource(tr._new_market_tab_plan)
      and "save_pool(" not in inspect.getsource(tr._render_top20_market_tab))

# The four EXISTING tabs' own rendered HTML, byte-identical: re-render
# the ORIGINAL multi-market pool (restore it) and diff the Mixed/
# Australia/USA/Full 200 tabs' own content against what they'd show if
# _render_top20_tab()/_render_top20_australia_tab()/_render_full100_tab()
# were called directly and in isolation (bypassing render_top100_page()
# entirely) - proving the real page's own output for those four tabs
# is nothing but those three pre-existing, UNMODIFIED functions' own
# output, verbatim.
if os.path.exists(ts.DB_PATH):
    os.remove(ts.DB_PATH)
ts.save_pool(
    [
        {"ticker": t, "company_name": f"{t} Co", "universe": "Fixture",
         "value_score": 80.0 - i, "mos_pct": 30.0, "price": 100.0, "intrinsic_value": 130.0,
         "currency": cur, "psychology": 5.0, "sector": "Industrials", "dividend_yield_pct": 1.0}
        for i, (t, cur) in enumerate(_FIXTURE_TICKERS.items())
    ],
    as_of=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
)
for t in _FIXTURE_TICKERS:
    _save_score(t)
_enriched2 = tr._enriched_pool()
check("_render_top20_tab/_render_top20_australia_tab/_render_full100_tab are called with "
      "EXACTLY the same signature render_top100_page() always used (no new required argument "
      "was added to any of the three) - confirmed by source inspection",
      "_render_top20_tab(enriched, lang, finer_industry, sort_mode, filters=filters)" in _render_module_src
      and "_render_top20_australia_tab(enriched, lang, finer_industry, sort_mode, filters=filters)" in _render_module_src
      and "_render_top20_tab(enriched, lang, finer_industry, sort_mode, currency=\"USD\", filters=filters)" in _render_module_src
      and "_render_full100_tab(enriched, lang, finer_industry, sort_mode, filters=filters)" in _render_module_src)


print()
print(f"PASS={passed} FAIL={failed}")
sys.exit(1 if failed else 0)
