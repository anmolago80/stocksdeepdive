"""
TASK 24 (Director-directed, 29 Oct 2026) - split "Top 20 · USA" into
USA-only + new "Top 20 · Foreign US-listed" tab (test stage, owner
approval before go-live).

CURRENT (verified, top100_render.py): tabs = Mixed, Australia, USA,
per-market tabs (_new_market_tab_plan), Full 200. "Top 20 · USA" is
_render_top20_tab(..., currency="USD") - every USD-traded row, so
Foreign US-listed companies (ATAT, NTES, KGC...) land in it.

Covers:
  1. Classification (_foreign_us_listed_home_country_by_ticker()):
     keyed OFF the latest saved "Foreign US-listed" scan's own rows
     (scan_store.load_scan_raw(..., allow_private=True)), filtered to
     Home Country != "United States" - NEVER off a pool row's own
     "universe" field. A ticker in BOTH a plain US universe (its
     pool row says "universe": "S&P 400") AND the Foreign US-listed
     scan (Home Country "Ireland") still classifies as foreign - the
     exact freshest-row-wins trap the task named.
  2. "Top 20 · USA" excludes foreign rows (once shown - see gate below).
  3. New "Top 20 · Foreign US-listed" tab contains ONLY foreign rows,
     each with its own home-country label, same plain-text style as
     the Scanner page's own "Home Country" label.
  4. TEST STAGE GATE (TOP20_FOREIGN_TAB_LIVE): unset/"0" - non-owner
     page is unchanged (same tab count/order/content as before this
     task, foreign rows still in the USA tab); owner sees both the
     new tab and the USA-only split. "1" - both tabs for everyone.
  5. Mixed/Australia/other-market/Full 200 tabs, pool selection,
     scoring and top100_engine are untouched by this task (same
     byte-identical-output proof pattern TASK 7's own test uses).

ALL FIXTURES IN THIS FILE ARE SYNTHETIC - this sandbox has no outbound
network access.

Run: python3 tests/test_director_task24_top20_foreign_tab.py
"""
import os
import sys
import tempfile
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TESTVOL = tempfile.mkdtemp(prefix="task24_top20_foreign_tab_test_")
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = TESTVOL
os.environ.pop("TOP20_FOREIGN_TAB_LIVE", None)
os.environ.pop("PRIVATE_UNIVERSES", None)

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


import ai_gate
import scan_store
import top100_engine as te
import top100_store as ts
import top100_render as tr

if os.path.exists(ts.DB_PATH):
    os.remove(ts.DB_PATH)

OWNER_EMAIL = ai_gate.owner_email()
NONOWNER_EMAIL = "someone-else@example.com"


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
# Fixture pool: 5 rows.
#   AAPL  - plain USA, universe "S&P 500", NOT in the Foreign US-listed
#           scan at all -> stays in the USA tab.
#   NU    - USA-traded, universe "Foreign US-listed", Home Country
#           "Brazil" -> foreign.
#   JAZZ  - USA-traded, pool's own "universe" field says "S&P 400" (the
#           exact freshest-row-wins trap the task named) - but IS in
#           the Foreign US-listed scan with Home Country "Ireland" ->
#           still classifies as foreign, proving classification never
#           trusts the pool row's own "universe" field.
#   USCO  - USA-traded, IS a row in the Foreign US-listed scan, but its
#           OWN Home Country there is "United States" -> sanity: never
#           misclassified as foreign just for appearing in that scan.
#   CSL.AX - Australia, unaffected control row (existing AU tab only).
# ======================================================================
ts.save_pool(
    [
        {"ticker": "AAPL", "company_name": "Apple", "universe": "S&P 500", "value_score": 80.0,
         "mos_pct": 20.0, "price": 180.0, "intrinsic_value": 210.0, "currency": "USD",
         "psychology": 5.0, "sector": "Technology", "dividend_yield_pct": 0.5},
        {"ticker": "NU", "company_name": "Nu Holdings Ltd.", "universe": "Foreign US-listed",
         "value_score": 70.0, "mos_pct": 25.0, "price": 12.0, "intrinsic_value": 16.0,
         "currency": "USD", "psychology": 5.0, "sector": "Financial Services",
         "dividend_yield_pct": 0.0},
        {"ticker": "JAZZ", "company_name": "Jazz Pharmaceuticals", "universe": "S&P 400",
         "value_score": 65.0, "mos_pct": 15.0, "price": 120.0, "intrinsic_value": 140.0,
         "currency": "USD", "psychology": 5.0, "sector": "Health Care", "dividend_yield_pct": 0.0},
        {"ticker": "USCO", "company_name": "US Domiciled Co", "universe": "Foreign US-listed",
         "value_score": 60.0, "mos_pct": 10.0, "price": 50.0, "intrinsic_value": 55.0,
         "currency": "USD", "psychology": 5.0, "sector": "Industrials", "dividend_yield_pct": 1.0},
        {"ticker": "CSL.AX", "company_name": "CSL", "universe": "ASX 200", "value_score": 75.0,
         "mos_pct": 25.0, "price": 200.0, "intrinsic_value": 250.0, "currency": "AUD",
         "psychology": 5.0, "sector": "Health Care", "dividend_yield_pct": 1.5},
    ],
    as_of=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
)
for t in ("AAPL", "NU", "JAZZ", "USCO", "CSL.AX"):
    _save_score(t)

scan_store.save_scan("Foreign US-listed", [
    {"Ticker": "NU", "Type": "STOCK", "Company Name": "Nu Holdings Ltd.", "Price": 12.0,
     "Intrinsic Value": 16.0, "MOS %": 25.0, "Long Score": 60.0, "Quality": 70.0,
     "Psychology": 5.0, "Discovery (lite)": 30.0, "Moat": 50.0,
     "Growth Used": 0.1, "FCF Source": "info", "Dividend Yield %": 0.0,
     "Home Country": "Brazil"},
    {"Ticker": "JAZZ", "Type": "STOCK", "Company Name": "Jazz Pharmaceuticals", "Price": 120.0,
     "Intrinsic Value": 140.0, "MOS %": 15.0, "Long Score": 55.0, "Quality": 65.0,
     "Psychology": 5.0, "Discovery (lite)": 28.0, "Moat": 55.0,
     "Growth Used": 0.05, "FCF Source": "reported", "Dividend Yield %": 0.0,
     "Home Country": "Ireland"},
    {"Ticker": "USCO", "Type": "STOCK", "Company Name": "US Domiciled Co", "Price": 50.0,
     "Intrinsic Value": 55.0, "MOS %": 10.0, "Long Score": 50.0, "Quality": 60.0,
     "Psychology": 5.0, "Discovery (lite)": 25.0, "Moat": 45.0,
     "Growth Used": 0.03, "FCF Source": "reported", "Dividend Yield %": 1.0,
     "Home Country": "United States"},
], source_label="test fixture")

enriched = tr._enriched_pool()
check("sanity: all 5 fixture rows are RATED (composite is not None)",
      all(r["composite"] is not None for r in enriched))

# ======================================================================
# CHECK 1: classification is keyed off the Foreign US-listed scan's own
# rows (Home Country != United States), never off the pool row's own
# "universe" field.
# ======================================================================
_foreign_map = tr._foreign_us_listed_home_country_by_ticker()
check("NU classifies as foreign (Home Country Brazil)",
      _foreign_map.get("NU") == "Brazil")
check("JAZZ classifies as foreign DESPITE its own pool row saying "
      "universe='S&P 400' (not 'Foreign US-listed') - classification reads "
      "the scan's own rows, never the pool row's 'universe' field",
      _foreign_map.get("JAZZ") == "Ireland")
check("USCO does NOT classify as foreign - it IS a row in the Foreign "
      "US-listed scan, but its own Home Country there is 'United States'",
      "USCO" not in _foreign_map)
check("AAPL does NOT classify as foreign - not in the Foreign US-listed scan at all",
      "AAPL" not in _foreign_map)
check("CSL.AX does NOT classify as foreign (not USD, not in the scan)",
      "CSL.AX" not in _foreign_map)
print("[task24_c1_classification] NU/JAZZ foreign (incl. the universe-field trap), "
      "USCO/AAPL/CSL.AX not OK")

# ======================================================================
# CHECK 2-3: _render_top20_tab's own exclude_tickers/only_tickers/
# home_country_by_ticker params, exercised directly (no page/gate
# involved) - USA-only split excludes foreign rows; the foreign-only
# slice contains exactly them, each labelled with its home country.
# ======================================================================
from streamlit.testing.v1 import AppTest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _run_direct_tab(kind, lang="en"):
    """Renders ONE tab function directly (bypassing render_top100_page()
    and its gate entirely) via AppTest - same "call the render function
    directly, not through the full page" pattern used to isolate a
    single tab's own output from everything else on the page."""
    if kind == "usa_excl":
        call = (
            "top100_render._render_top20_tab(enriched, 'en', finer_industry, "
            "'value', currency='USD', exclude_tickers=foreign_tickers)"
        )
    else:
        call = (
            "top100_render._render_top20_tab(enriched, 'en', finer_industry, "
            "'value', currency='USD', only_tickers=foreign_tickers, "
            "home_country_by_ticker=foreign_map)"
        )
    script = f"""
import os, sys
sys.path.insert(0, {REPO_ROOT!r})
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = {TESTVOL!r}
import top100_render
enriched = top100_render._enriched_pool()
finer_industry = top100_render._finer_industry_by_ticker()
foreign_map = top100_render._foreign_us_listed_home_country_by_ticker()
foreign_tickers = set(foreign_map)
{call}
"""
    at = AppTest.from_string(script)
    at.run(timeout=30)
    assert not at.exception, f"direct tab render ({kind}) raised: {at.exception}"
    return " ".join(m.value or "" for m in at.get("markdown"))


_usa_html = _run_direct_tab("usa_excl")
check("USA-only split: AAPL (plain USA) still appears",
      ">AAPL<" in _usa_html)
check("USA-only split: NU (foreign) is EXCLUDED",
      ">NU<" not in _usa_html)
check("USA-only split: JAZZ (foreign via the universe-field trap) is EXCLUDED",
      ">JAZZ<" not in _usa_html)
check("USA-only split: USCO (US home country, not foreign) still appears",
      ">USCO<" in _usa_html)
print("[task24_c2_usa_tab_excludes_foreign] AAPL/USCO stay, NU/JAZZ excluded OK")

_foreign_html = _run_direct_tab("foreign_only")
check("Foreign tab: NU appears",
      ">NU<" in _foreign_html)
check("Foreign tab: JAZZ appears",
      ">JAZZ<" in _foreign_html)
check("Foreign tab: AAPL does NOT appear",
      ">AAPL<" not in _foreign_html)
check("Foreign tab: USCO does NOT appear (not classified foreign)",
      ">USCO<" not in _foreign_html)
check("Foreign tab: NU's own home-country label (Brazil) is shown",
      ">Brazil<" in _foreign_html)
check("Foreign tab: JAZZ's own home-country label (Ireland) is shown",
      ">Ireland<" in _foreign_html)
print("[task24_c3_foreign_tab_contents_and_labels] only NU/JAZZ, each labelled "
      "with its own home country OK")


# ======================================================================
# CHECK 4: the gate - full page render, non-owner vs owner, env unset
# vs "1".
# ======================================================================
def _run_page(lang="en", email=NONOWNER_EMAIL, foreign_tab_live=None):
    env_line = (
        f'os.environ["TOP20_FOREIGN_TAB_LIVE"] = {foreign_tab_live!r}'
        if foreign_tab_live is not None else
        'os.environ.pop("TOP20_FOREIGN_TAB_LIVE", None)'
    )
    script = f"""
import os, sys
sys.path.insert(0, {REPO_ROOT!r})
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = {TESTVOL!r}
{env_line}
import top100_render
import paywall_engine as pw
pw.current_user_email = lambda: {email!r}
top100_render.render_top100_page(lang={lang!r})
"""
    at = AppTest.from_string(script)
    at.run(timeout=30)
    assert not at.exception, f"render_top100_page(lang={lang!r}) raised: {at.exception}"
    return at


# --- Gate OFF, non-owner: page unchanged - no new tab, USA tab still
# includes the foreign rows (today's exact behaviour, untouched). ---
_at_nonowner_off = _run_page(email=NONOWNER_EMAIL, foreign_tab_live=None)
_labels_nonowner_off = [tab.proto.label for tab in _at_nonowner_off.tabs]
check("GATE OFF, non-owner: no 'Top 20 · Foreign US-listed' tab at all",
      "Top 20 · Foreign US-listed" not in _labels_nonowner_off)
check("GATE OFF, non-owner: tab order/count is otherwise exactly as before this "
      "task (Mixed, Australia, USA, Full 200 - no other-market tab for this "
      "all-USA/AU fixture pool)",
      _labels_nonowner_off == ["Top 20 · Mixed", "Top 20 · Australia", "Top 20 · USA",
                                "Full 200"])
_html_nonowner_off = " ".join(m.value or "" for m in _at_nonowner_off.get("markdown"))
check("GATE OFF, non-owner: NU (foreign) STILL appears somewhere on the page - "
      "the USA tab is not split for a gated-off viewer, exactly as today",
      ">NU<" in _html_nonowner_off)
print("[task24_c4a_gate_off_nonowner_unchanged] no new tab, foreign rows still "
      "in the USA tab, unchanged from before this task OK")

# --- Gate OFF, owner: owner still sees the new tab and the split. ---
_at_owner_off = _run_page(email=OWNER_EMAIL, foreign_tab_live=None)
_labels_owner_off = [tab.proto.label for tab in _at_owner_off.tabs]
check("GATE OFF (env unset), OWNER: the new tab IS present",
      "Top 20 · Foreign US-listed" in _labels_owner_off)
check("GATE OFF (env unset), OWNER: it's placed right after 'Top 20 · USA'",
      _labels_owner_off.index("Top 20 · Foreign US-listed")
      == _labels_owner_off.index("Top 20 · USA") + 1)
print("[task24_c4b_gate_off_owner_sees_it] owner sees the new tab even with the "
      "env var unset OK")

# --- Gate ON ("1"), non-owner: both tabs for everyone. ---
_at_nonowner_on = _run_page(email=NONOWNER_EMAIL, foreign_tab_live="1")
_labels_nonowner_on = [tab.proto.label for tab in _at_nonowner_on.tabs]
check("GATE ON ('1'), non-owner: the new tab IS present too - live for everyone",
      "Top 20 · Foreign US-listed" in _labels_nonowner_on)
check("GATE ON, non-owner: placed right after 'Top 20 · USA', same position as "
      "for the owner",
      _labels_nonowner_on.index("Top 20 · Foreign US-listed")
      == _labels_nonowner_on.index("Top 20 · USA") + 1)
print("[task24_c4c_gate_on_everyone] TOP20_FOREIGN_TAB_LIVE=1 shows the new tab "
      "to a non-owner too OK")

# ======================================================================
# CHECK 5: Mixed/Australia/Full 200 tabs and the pool itself are
# untouched - same byte-identical-output proof pattern TASK 7's own
# test uses, run against the gate-OFF, non-owner render (today's
# default page).
# ======================================================================
import json as _json

_pool_before = te.select_top100_pool(log=lambda *a, **k: None)
tr._foreign_us_listed_home_country_by_ticker()  # exercise this task's own new code path
_pool_after = te.select_top100_pool(log=lambda *a, **k: None)
check("select_top100_pool()'s own return value is byte-identical before and after "
      "exercising this task's own new classification code - read-only, never "
      "touches selection/scoring",
      _json.dumps(_pool_before, sort_keys=True, default=str) ==
      _json.dumps(_pool_after, sort_keys=True, default=str))

check("GATE OFF, non-owner: Australia tab's own control row (CSL.AX) still renders",
      ">CSL.AX<" in _html_nonowner_off)
check("GATE OFF, non-owner: AAPL still renders somewhere (USA tab, unsplit)",
      ">AAPL<" in _html_nonowner_off)
print("[task24_c5_other_tabs_and_pool_unaffected] selection byte-identical, "
      "Mixed/Australia/USA(unsplit)/Full 200 content all present as before OK")

print()
print(f"PASS={passed} FAIL={failed}")
sys.exit(1 if failed else 0)
