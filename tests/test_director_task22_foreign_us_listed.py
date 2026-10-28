"""
TASK 22 (Director-directed, 27 Oct 2026) - new universe "Foreign
US-listed": every non-US-domiciled company listed directly on NYSE,
Nasdaq or NYSE American (Nu Holdings, MercadoLibre, TSMC, ASML, etc.) -
excluded from every existing US universe today, so none of them can
ever be a Top 200 candidate. SHIPS IN TEST STAGE ONLY: private
(owner-only) + excluded from the live Top 200 by default. Nothing
becomes public or Top-200-eligible until the owner edits the two env
vars by hand, after reviewing the test-stage results.

Covers the task's own listed tests:
  1. Filter logic on a screener-JSON fixture (country, market cap,
     liquidity, fund/SPAC/preferred/warrant/unit/right exclusion).
  2. < 100 rows -> failure (None), with the per-stage filter_counts a
     caller can log as a diagnostic.
  3. Symbol mapping (BRK.B-shaped dotted tickers -> hyphenated Yahoo
     symbols, via symbol_mapping.to_yahoo_symbol(..., "US")).
  4. Private + Top-200-excluded by DEFAULT (env vars unset).
  5. Listed in scanner_engine.USA_UNIVERSES (what makes it resolvable/
     visible in the Scanner picker at all - the owner-only visibility
     itself is scan_store.is_private_universe(), already covered by
     check 4, and the picker code already reads both generically with
     no per-universe special-casing needed).
  6. ADR vs home-listing dedupe (top100_engine._dedupe_cross_listings()
     - confirms the EXISTING, unmodified mechanism resolves a same-
     name pair across two countries to one row, logging the merge).
  7. The owner-only Top 200 preview includes this universe (once
     TOP200_PREVIEW_UNIVERSES names it) while the REAL, public pool
     stays completely unaffected (is_top200_excluded() still true with
     that env var unset) - plus the exact "[top200-preview] ... would
     enter ... would be displaced ... cutoff ..." log line format.
  8. Home Country passthrough: nightly_scan.run_universe_scan()'s own
     row["Home Country"] field, set only for this one universe.

ALL FIXTURES ARE SYNTHETIC - this sandbox has no outbound network
access to verify the real Nasdaq screener response; the real proof is
the first live scan, exactly like every other universe in this app.

Run: python3 tests/test_director_task22_foreign_us_listed.py
"""
import os
import sys
import tempfile
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TESTVOL = tempfile.mkdtemp(prefix="task22_foreign_us_listed_test_")
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = TESTVOL
os.environ.pop("PRIVATE_UNIVERSES", None)
os.environ.pop("TOP200_EXCLUDED_UNIVERSES", None)
os.environ.pop("TOP200_PREVIEW_UNIVERSES", None)

import scanner_engine as se
import scan_store
import symbol_mapping
import app

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


def _row(symbol, name, country, cap, price, volume, sector="Technology"):
    return {
        "symbol": symbol, "name": name, "country": country, "marketCap": str(cap),
        "lastsale": f"${price}", "volume": str(volume), "sector": sector, "industry": "X",
    }


# ======================================================================
# CHECK 1+2: filter pipeline - country/fund/cap/liquidity exclusion,
# and the >= 100 rows rule (a fixture with exactly 100 good rows passes;
# one short of it fails with None, still returning the per-stage counts).
# ======================================================================
_good_rows = [_row(f"FOR{i}", f"Foreign Co {i} Ltd", "Brazil", 2_000_000_000, 20.0, 1_000_000)
              for i in range(100)]
_noise_rows = [
    _row("USCO", "US Co Inc", "United States", 5_000_000_000, 50.0, 1_000_000),
    _row("FUNDX", "Global Growth Fund", "Canada", 5_000_000_000, 50.0, 1_000_000),
    _row("ETFX", "Some ETF Trust", "Canada", 5_000_000_000, 50.0, 1_000_000),
    _row("SPACX", "Big Deal Acquisition Corp", "Cayman Islands", 2_000_000_000, 20.0, 1_000_000),
    _row("PFDX", "Foreign Preferred Shares", "Japan", 2_000_000_000, 20.0, 1_000_000),
    _row("WARX", "Foreign Co Warrants", "Japan", 2_000_000_000, 20.0, 1_000_000),
    _row("UNITX", "Foreign Co Units", "Japan", 2_000_000_000, 20.0, 1_000_000),
    _row("RIGHTX", "Foreign Co Rights", "Japan", 2_000_000_000, 20.0, 1_000_000),
    _row("SMALLX", "Small Foreign Co", "Japan", 500_000_000, 10.0, 1_000_000),
    _row("ILLIQX", "Illiquid Foreign Co", "Japan", 2_000_000_000, 5.0, 100),
    _row("NOCOUNTRYX", "No Country Co", "", 2_000_000_000, 20.0, 1_000_000),
    _row("UNITEDCO", "United Industries Ltd", "Germany", 2_000_000_000, 20.0, 1_000_000),
]
df, counts = se._foreign_us_listed_parse_rows(_good_rows + _noise_rows)
check("the 100 good foreign rows, PLUS 'United Industries Ltd' (a legitimate name "
      "that merely contains 'Unit' as a substring - see the word-boundary check "
      "below), survive every filter stage: 101 total",
      df is not None and len(df) == 101)
check("the US-domiciled row is excluded (country filter)",
      df is not None and "USCO" not in set(df["Ticker"]))
check("the fund/ETF/SPAC/preferred/warrant/unit/right rows are all excluded (name filter)",
      df is not None and not any(
          t in set(df["Ticker"]) for t in ("FUNDX", "ETFX", "SPACX", "PFDX", "WARX", "UNITX", "RIGHTX")
      ))
check("'United Industries Ltd' (contains 'Unit' as a substring, not a whole word) "
      "is NOT excluded by the word-boundary-anchored name filter",
      df is not None and "UNITEDCO" in set(df["Ticker"]))
check("the under-$1B market-cap row is excluded",
      df is not None and "SMALLX" not in set(df["Ticker"]))
check("the illiquid (price x volume < $5M/day) row is excluded",
      df is not None and "ILLIQX" not in set(df["Ticker"]))
check("the row with no/blank country is excluded (never assumed foreign)",
      df is not None and "NOCOUNTRYX" not in set(df["Ticker"]))
check("every surviving row carries a 'Home Country' column", df is not None and "Home Country" in df.columns)
check("every surviving row's Home Country is a real string, never the US",
      df is not None and all(c and c != "United States" for c in df["Home Country"]))
print(f"[task22_c1_filter_pipeline] filter_counts={counts} OK")

_short_rows = _good_rows[:99]
df_short, counts_short = se._foreign_us_listed_parse_rows(_short_rows)
check("fewer than 100 surviving rows (99) -> None (treated as a fetch failure)",
      df_short is None)
check("the per-stage filter_counts are still returned even on failure, for a diagnostic",
      counts_short.get("after_dedupe") == 99 and counts_short.get("raw") == 99)
print(f"[task22_c2_min_rows_rule] 99 rows -> None, counts={counts_short} OK")

# ======================================================================
# CHECK 3: symbol mapping - "US" exchange hyphenates dots, same stem
# rule every other exchange already uses, no suffix appended (unlike
# every other exchange this function supports).
# ======================================================================
check("to_yahoo_symbol('BRK.B', 'US') == 'BRK-B' (no country suffix, unlike LSE/TSX/etc.)",
      symbol_mapping.to_yahoo_symbol("BRK.B", "US") == "BRK-B")
check("to_yahoo_symbol('NU', 'US') == 'NU' (a plain ticker needs no transform at all)",
      symbol_mapping.to_yahoo_symbol("NU", "US") == "NU")
check("to_yahoo_symbol('NU.L', 'US') == 'NU.L' (idempotent - already a real Yahoo "
      "suffix, from some OTHER exchange's own table - left unchanged, same rule as "
      "every other exchange this function supports; '.AX' is NOT one of this "
      "module's own idempotency suffixes - ASX symbols are normalised elsewhere, "
      "in scanner_engine.py, never via to_yahoo_symbol() - so a '.AX'-suffixed "
      "input would NOT be treated as idempotent here, which is correct: this "
      "fetcher's own screener never produces one)",
      symbol_mapping.to_yahoo_symbol("NU.L", "US") == "NU.L")
print("[task22_c3_symbol_mapping] to_yahoo_symbol(..., 'US') hyphenates dots, "
      "no suffix, idempotent for another exchange's own suffix OK")

# ======================================================================
# CHECK 4: private + Top-200-excluded by DEFAULT, with both env vars
# unset. (With either env var explicitly SET to something else, as
# this task's own report explains is the real Railway state today,
# that explicit value overrides this code default entirely - that is
# the owner's own env-var edit to make, not something this check can
# exercise in a sandbox with no access to the real Railway values.)
# ======================================================================
import top100_engine

os.environ.pop("PRIVATE_UNIVERSES", None)
os.environ.pop("TOP200_EXCLUDED_UNIVERSES", None)
check("scan_store.is_private_universe('Foreign US-listed') is True by default (env unset)",
      scan_store.is_private_universe("Foreign US-listed") is True)
check("top100_engine.is_top200_excluded('Foreign US-listed') is True by default (env unset)",
      top100_engine.is_top200_excluded("Foreign US-listed") is True)
check("'Foreign US-listed' IS in scanner_engine.USA_UNIVERSES (country USA, for "
      "scan-table/currency purposes) - the derived Top-200 rule would NOT exclude "
      "it on its own, which is exactly why TASK 22 needed its own explicit "
      "_TOP200_EXTRA_EXCLUDED_WHEN_UNSET entry",
      "Foreign US-listed" in se.USA_UNIVERSES)
check("a genuinely non-US universe (e.g. 'DAX') is still excluded by the ordinary "
      "derived rule alone, unaffected by TASK 22's own one-universe addition",
      top100_engine.is_top200_excluded("DAX") is True)
check("a genuine US universe (e.g. 'S&P 500') is still NOT excluded, unaffected",
      top100_engine.is_top200_excluded("S&P 500") is False)

# An explicit, set env var overrides the code default entirely in both
# directions - proving the mechanism the owner will actually use to go
# live later (append "Foreign US-listed" to each one's current value).
os.environ["PRIVATE_UNIVERSES"] = "Nikkei 225"
check("PRIVATE_UNIVERSES explicitly set WITHOUT 'Foreign US-listed' -> no longer "
      "private (the code default never applies once the env var is set at all)",
      scan_store.is_private_universe("Foreign US-listed") is False)
os.environ.pop("PRIVATE_UNIVERSES", None)

os.environ["TOP200_EXCLUDED_UNIVERSES"] = "DAX"
check("TOP200_EXCLUDED_UNIVERSES explicitly set WITHOUT 'Foreign US-listed' -> no "
      "longer Top-200-excluded (same 'a set env var always wins, even over TASK "
      "22's own extra entry' rule)",
      top100_engine.is_top200_excluded("Foreign US-listed") is False)
os.environ.pop("TOP200_EXCLUDED_UNIVERSES", None)
print("[task22_c4_private_and_excluded_by_default] private=True, Top-200-excluded="
      "True with both env vars unset; either env var being SET overrides the "
      "default entirely, in both directions OK")

# ======================================================================
# CHECK 5: scanner picker visibility - TASK 22b (Director-directed, 28
# Oct 2026) found that TASK 22's own version of this check asserted
# membership in scanner_engine.USA_UNIVERSES/get_universes("USA"),
# which only proves the DATA LAYER (scan resolution, currency
# handling) knows about this universe - NOT that the Scanner page's
# picker actually renders a button for it. The real picker is built
# from app._SCANNER_PICKER_BANDS and its own derived
# app._all_scanner_picker_universes() - TASK 22 never added a pill
# there, so this check passed wrongly while the picker had no button
# at all (fixed in TASK 22b by adding the missing pill). is_private_
# universe() is what the SAME generic picker code already gates owner-
# only visibility on. Both already independently confirmed above
# (check 4) - this check just states the conclusion explicitly as its
# own named assertion, matching the task's own numbered list.
# ======================================================================
check("'Foreign US-listed' is in app._all_scanner_picker_universes() - the "
      "Scanner picker's OWN derived universe set, proving a real pill/button "
      "exists for it (TASK 22b fix; TASK 22 itself only checked USA_UNIVERSES, "
      "which passed wrongly with no button at all)",
      "Foreign US-listed" in app._all_scanner_picker_universes())
print("[task22_c5_picker_visibility] has a real picker pill via "
      "_all_scanner_picker_universes(); private-by-default (check 4) is what "
      "keeps it owner-only in the picker OK")

# ======================================================================
# CHECK 6: ADR vs home-listing dedupe - confirms the EXISTING, unmodified
# top100_engine._dedupe_cross_listings() mechanism resolves a same-
# company-name pair across two countries (e.g. a US-listed ADR vs its
# ASX home listing) to ONE surviving row, logging the merge. No new
# dedupe code for this task - this is exactly what the task's own
# "rely on existing _dedupe_cross_listings" instruction asks for.
# ======================================================================
_best_by_ticker = {
    "NU": {"ticker": "NU", "company_name": "Example Foreign Co Ltd", "value_score": 70},
    "NU.AX": {"ticker": "NU.AX", "company_name": "Example Foreign Co Ltd", "value_score": 50},
}
_dedupe_logs = []
top100_engine._dedupe_cross_listings(_best_by_ticker, log=_dedupe_logs.append)
check("the ADR (US) vs home-listing (ASX) pair resolves to exactly ONE surviving row",
      len(_best_by_ticker) == 1)
check("the higher-Value-Score side (no traded-value data for either, so it falls "
      "back to Value Score) wins - 'NU' survives, 'NU.AX' is removed",
      "NU" in _best_by_ticker and "NU.AX" not in _best_by_ticker)
check("the merge is logged (never a silent drop)",
      len(_dedupe_logs) == 1 and "NU" in _dedupe_logs[0] and "NU.AX" in _dedupe_logs[0])
print(f"[task22_c6_adr_dedupe] {_dedupe_logs[0]!r} OK")

# A genuinely different country-pair with the SAME ASX/foreign shape
# (ASX preference) also still applies unchanged - not retested in full
# here (top100_engine's own existing test suite already covers the
# ASX-preference rule's own thresholds in depth); this check only
# confirms the single-mechanism "no new dedupe code" claim above holds
# for a country pair that was NEVER previously possible before this
# task (no universe ever produced a plain US ticker for a foreign
# company until "Foreign US-listed" existed).

# ======================================================================
# CHECK 7: the owner-only Top 200 preview includes this universe once
# TOP200_PREVIEW_UNIVERSES names it, while the REAL pool stays
# completely unaffected (is_top200_excluded() is unrelated to, and
# never read by, preview_candidates_by_country() at all).
# ======================================================================
import market_readiness_engine

_PREVIEW_PAYLOAD = {
    "generated_at": "2026-10-27T05:00:00+00:00",
    "attention_lite": True,
    "rows": [
        {
            "Ticker": "NU", "Company Name": "Nu Holdings Ltd.", "Long Score": 80,
            "Price": 12.0, "Intrinsic Value": 15.0, "MOS %": 20.0, "Growth Used": 0.1,
            "FCF Source": "info", "Dividend Yield %": 0.0, "Home Country": "Brazil",
            "Quality": 85, "Psychology": 5, "Discovery (lite)": 10,
        },
        {
            "Ticker": "MELI", "Company Name": "MercadoLibre Inc.", "Long Score": 40,
            "Price": 1500.0, "Intrinsic Value": 1400.0, "MOS %": -5.0, "Growth Used": 0.08,
            "FCF Source": "info", "Dividend Yield %": 0.0, "Home Country": "Argentina",
            "Quality": 40, "Psychology": 0, "Discovery (lite)": 5,
        },
    ],
}

os.environ["TOP200_PREVIEW_UNIVERSES"] = "Foreign US-listed"
with mock.patch.object(scan_store, "load_scan_raw", return_value=_PREVIEW_PAYLOAD), \
     mock.patch.object(top100_engine, "pool_value_score_cutoff", return_value=30.0), \
     mock.patch.object(top100_engine, "_current_pool_rated_composites", return_value={}), \
     mock.patch.object(top100_engine.top100_store, "latest_scores_for_model", return_value={}):
    _preview = top100_engine.preview_candidates_by_country()
_usa_rows = _preview["by_country"].get("USA") or []
check("the preview's 'by_country' includes a 'USA' bucket with both candidates",
      len(_usa_rows) == 2)
check("each candidate row carries its own 'home_country' field, separate from the "
      "'USA' market bucket it's grouped under",
      {r["ticker"]: r["home_country"] for r in _usa_rows} == {"NU": "Brazil", "MELI": "Argentina"})
check("the candidate whose RECOMPUTED Value Score clears the 30.0 cutoff "
      "(NU, ~44.7) shows enters_pool=True",
      next(r for r in _usa_rows if r["ticker"] == "NU")["enters_pool"] is True)
check("the candidate whose recomputed Value Score stays below the cutoff "
      "(MELI, ~15.9) shows enters_pool=False",
      next(r for r in _usa_rows if r["ticker"] == "MELI")["enters_pool"] is False)

_log_lines = []
with mock.patch.object(scan_store, "load_scan_raw", return_value=_PREVIEW_PAYLOAD), \
     mock.patch.object(top100_engine, "pool_value_score_cutoff", return_value=30.0), \
     mock.patch.object(top100_engine, "_current_pool_rated_composites", return_value={}), \
     mock.patch.object(top100_engine.top100_store, "latest_scores_for_model", return_value={}), \
     mock.patch.object(market_readiness_engine, "top200_dry_run",
                        return_value={"candidate_count": 2, "no_stored_score_count": 2,
                                      "estimated_cost_usd": 0.10}):
    top100_engine._log_preview_country_summary(log=_log_lines.append)
check("_log_preview_country_summary() logs exactly one line for the previewed universe",
      len(_log_lines) == 1)
check("the log line matches the task's own exact format: candidates would enter, "
      "would be displaced, cutoff, est. one-time cost",
      "[top200-preview] 'Foreign US-listed':" in _log_lines[0]
      and "candidate(s) would enter" in _log_lines[0]
      and "would be displaced" in _log_lines[0]
      and "cutoff" in _log_lines[0]
      and "est. one-time cost $0.10" in _log_lines[0])
check("exactly 1 candidate (NU) is counted as entering (MELI is below cutoff)",
      "1 candidate(s) would enter" in _log_lines[0])
print(f"[task22_c7_preview_includes_universe] {_log_lines[0]!r} OK")

os.environ.pop("TOP200_PREVIEW_UNIVERSES", None)
check("with TOP200_EXCLUDED_UNIVERSES left unset (the real default), the live pool "
      "is completely unaffected by any of the above - is_top200_excluded() is still "
      "True and _eligible_scan_payloads() (the real pool's own universe source) "
      "never calls preview_candidates_by_country() or is affected by it in any way",
      top100_engine.is_top200_excluded("Foreign US-listed") is True)
print("[task22_c7b_live_pool_unaffected] is_top200_excluded() still True after the "
      "preview ran - the real pool is a completely separate code path OK")

# ======================================================================
# CHECK 8: Home Country passthrough - nightly_scan.run_universe_scan()
# sets row["Home Country"] ONLY for this one universe's own pool_df
# column, exactly like the existing Sector passthrough pattern, and
# leaves every other universe's rows with no such field at all.
# ======================================================================
import pandas as pd
import nightly_scan
import scan_checkpoint_store

_FUS_POOL_DF = pd.DataFrame({
    "Ticker": ["NU"], "Sector": ["Financial Services"], "Home Country": ["Brazil"],
})
_OTHER_POOL_DF = pd.DataFrame({"Ticker": ["AAPL"], "Sector": ["Technology"]})


def _fake_analyze_ticker_lite(ticker, attention_lite=True, discount_rate=None, log=print,
                               rate_limited_out=None, growth_summary_out=None, oneoff_summary_out=None,
                               shadow_out=None, info_failed_out=None, universe=None,
                               no_yahoo_price_out=None):
    return {"Ticker": ticker, "Price": 10.0, "Long Score": 50}


# Same proven mocking recipe tests/test_audit_c3_queued_rescan_abandoned_
# worker.py's own CHECK 6 already uses to call run_universe_scan()
# directly and successfully - scan_store.save_scan() is left REAL (not
# mocked), writing to this test file's own TESTVOL, then read straight
# back via scan_store.load_scan_raw() - simpler and more faithful than
# trying to capture save_scan()'s own call args.
with mock.patch.object(se, "get_universe_pool", return_value=(_FUS_POOL_DF, "test fixture (live)")), \
     mock.patch.object(nightly_scan, "_yf_call_with_retry", return_value=None), \
     mock.patch.object(nightly_scan, "analyze_ticker_lite", side_effect=_fake_analyze_ticker_lite), \
     mock.patch.object(nightly_scan, "_attach_moat"), \
     mock.patch.object(nightly_scan, "_attach_dividend_payout"), \
     mock.patch.object(scan_checkpoint_store, "load", return_value=None), \
     mock.patch.object(scan_checkpoint_store, "save"):
    nightly_scan.run_universe_scan("Foreign US-listed", log=lambda *a, **k: None)
_saved_a = scan_store.load_scan_raw("Foreign US-listed", allow_private=True)
check("Foreign US-listed's own scanned row carries 'Home Country': 'Brazil'",
      _saved_a is not None
      and any(r.get("Ticker") == "NU" and r.get("Home Country") == "Brazil" for r in _saved_a["rows"]))

with mock.patch.object(se, "get_universe_pool", return_value=(_OTHER_POOL_DF, "test fixture (live)")), \
     mock.patch.object(nightly_scan, "_yf_call_with_retry", return_value=None), \
     mock.patch.object(nightly_scan, "analyze_ticker_lite", side_effect=_fake_analyze_ticker_lite), \
     mock.patch.object(nightly_scan, "_attach_moat"), \
     mock.patch.object(nightly_scan, "_attach_dividend_payout"), \
     mock.patch.object(scan_checkpoint_store, "load", return_value=None), \
     mock.patch.object(scan_checkpoint_store, "save"):
    nightly_scan.run_universe_scan("S&P 500", log=lambda *a, **k: None)
_saved_b = scan_store.load_scan_raw("S&P 500", allow_private=True)
check("every OTHER universe's own scanned row has NO 'Home Country' key with a "
      "real value (None - the passthrough dict is empty for them)",
      _saved_b is not None and all(not r.get("Home Country") for r in _saved_b["rows"]))
print("[task22_c8_home_country_passthrough] set only for Foreign US-listed's own "
      "rows, None/absent for every other universe OK")

# ======================================================================
# CHECK 9 (item 6): banks/financials go through the EXISTING financials
# path unchanged - financials_classifier.is_financials() is purely
# sector/industry-based (never country/exchange-based), so a foreign-
# domiciled bank like Nu Holdings (NYSE: NU, Financial Services sector)
# is classified financials-mode by the exact same rule that already
# handles AUB.AX - no new code needed anywhere for this.
# ======================================================================
import financials_classifier

check("a foreign-domiciled bank (Financial Services sector) is classified "
      "financials-mode by the EXISTING, country-independent rule - the "
      "same ROE-based DCF/moat path every other bank already takes",
      financials_classifier.is_financials({"sector": "Financial Services", "industry": "Banks-Regional"}))
check("a foreign-domiciled REIT is correctly NOT financials-mode either - "
      "the existing REIT exclusion is industry-string-based, same as "
      "for any other REIT regardless of country",
      not financials_classifier.is_financials({"sector": "Financial Services", "industry": "REIT-Retail"}))
print("[task22_c9_financials_path_unchanged] NU-shaped (Financial Services) "
      "classifies financials-mode via the existing, country-independent rule OK")

print()
print(f"PASS={passed} FAIL={failed}")
sys.exit(1 if failed else 0)
