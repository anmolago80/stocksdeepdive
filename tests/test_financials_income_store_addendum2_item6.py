"""
Addendum 2 item 6 (Director, 5 Oct 2026) of instruction_financials_
income_store_and_top200_guard.md. Item 4's own finding: run_nightly_
prepass()'s own Way 2 fill never set an entry's `latest_cf_period`, so
refresh_if_newer()'s old field-comparison staleness check could never
fire for any entry filled that way - which was every one of the 229
live entries as of 4-5 Oct 2026.

This commit makes staleness independent of how an entry was saved:
refresh_if_newer() now compares the latest period end IN THE STORED
INCOME TABLE ITSELF against the fresh cash-flow statement's latest
period, with no dependency on the `latest_cf_period` field at all -
so it automatically covers every existing entry, pre-pass-saved or
not, with no migration step.

Also: run_nightly_prepass()'s own Way 2 (cached bundle) no longer
accepts an income table it can tell, right there, is already older
than that SAME cached bundle's own cash-flow statement - and still
accepts it when that comparison isn't possible (bundle has no cached
cashflow), leaving rule 1 to catch it later if it's wrong.

ALL FIXTURES IN THIS FILE ARE SYNTHETIC - this sandbox has no outbound
network access, same disclosure as every other fixture-based test in
this repo. No test here makes a live network call.

Run: python3 tests/test_financials_income_store_addendum2_item6.py
"""
import os
import sys
import tempfile
from unittest import mock

import pandas as pd

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

TESTVOL = tempfile.mkdtemp(prefix="financials_addendum2_item6_test_")
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = TESTVOL
os.environ.pop("FINANCIALS_STORE_LIVE", None)

import financials_income_store as fis
import financials_dry_run as fdr
import fundamentals_data
import scan_store
import capm_engine


def _mk_income_df(net_income, cols):
    return pd.DataFrame({"Net Income Common Stockholders": net_income}, index=cols).T


def _mk_cf_df(ocf, capex, cols):
    return pd.DataFrame({"Operating Cash Flow": ocf, "Capital Expenditure": capex}, index=cols).T


def _mk_rows(tickers_scores, mode="financials"):
    return [{"Ticker": t, "Long Score": s, "Moat Mode": mode} for t, s in tickers_scores]


def _clean():
    for f in os.listdir(fis._store_dir()):
        os.remove(os.path.join(fis._store_dir(), f))
    for f in os.listdir(scan_store._data_dir()):
        os.remove(os.path.join(scan_store._data_dir(), f))


_OLD_INCOME = _mk_income_df([100.0, 95.0, 90.0], ["2024-12-31", "2023-12-31", "2022-12-31"])
_NEW_INCOME = _mk_income_df([120.0, 100.0, 95.0], ["2025-12-31", "2024-12-31", "2023-12-31"])


# ======================================================================
# Setup: two tickers, each filled the SAME way run_nightly_prepass()'s
# own Way 2 fills a real entry today - a cached bundle with an income
# table but NO cashflow key at all (so the "known stale?" comparison in
# item 3's own fix below correctly can't be made, and the fill proceeds
# exactly as it does for a real Way-2-filled entry) - confirms the
# resulting entry's own latest_cf_period field is None, same as every
# one of the 229 live entries item 4 found.
# ======================================================================
_clean()
scan_store.save_scan("S&P 500", _mk_rows([("STALETICK", 90), ("FRESHTICK", 80)]),
                      source_label="fixture")

_old_bundle_no_cf = {"income": _OLD_INCOME, "info": {"currency": "USD"},
                     "meta": {"source": "yfinance"}}
with mock.patch.object(fis.fundamentals_data, "peek_cached_bundle", return_value=_old_bundle_no_cf):
    _setup_result = fis.run_nightly_prepass(["S&P 500"], log=lambda *a: None, budget=1000)
assert _setup_result["from_store"] == 2, _setup_result
for t in ("STALETICK", "FRESHTICK"):
    _entry = fis.get(t)
    assert _entry is not None, t
    assert _entry["source"] == "yfinance_bundle_cached", _entry["source"]
    assert _entry.get("latest_cf_period") is None, (
        f"{t}: run_nightly_prepass()'s own Way 2 fill must still never set latest_cf_period - "
        "confirms this fixture matches every one of item 4's 229 real entries")
print("[setup_pre_pass_saved_entries] STALETICK and FRESHTICK both filled via run_nightly_"
      "prepass()'s own Way 2 path, latest_cf_period=None on both (exactly item 4's finding) OK")


# ======================================================================
# T1: a pre-pass-saved entry goes stale when the cash flow shows a
# newer year - the main scan loop's own refresh_if_newer() call, now
# reading the stored income table's OWN latest period ("2024-12-31")
# instead of the never-set latest_cf_period field.
# ======================================================================
assert not fis.get("STALETICK").get("stale")
fis.refresh_if_newer("STALETICK", "2025-12-31")  # tonight's cash flow is a year newer
_stale_entry = fis.get("STALETICK")
assert _stale_entry.get("stale") is True, _stale_entry
print("[T1_newer_cashflow_marks_stale] STALETICK's stored income table tops out at "
      "2024-12-31; tonight's cash flow shows 2025-12-31 -> refresh_if_newer() marks it stale, "
      "with NO latest_cf_period field to have relied on OK")


# ======================================================================
# T2: a same-year entry does not go stale.
# ======================================================================
assert not fis.get("FRESHTICK").get("stale")
fis.refresh_if_newer("FRESHTICK", "2024-12-31")  # tonight's cash flow is the SAME year
_fresh_entry = fis.get("FRESHTICK")
assert _fresh_entry.get("stale") is not True, _fresh_entry
print("[T2_same_year_cashflow_not_stale] FRESHTICK's stored income table and tonight's cash "
      "flow both top out at 2024-12-31 -> refresh_if_newer() leaves it alone OK")


# ======================================================================
# T3: a stale entry is still used meanwhile (its data is untouched by
# mark_stale()), and is refetched by the next pre-pass.
# ======================================================================
_still_used = fis.get("STALETICK")
assert list(_still_used["income"].columns)[0] == "2024-12-31", (
    "a stale entry's OWN data must be untouched - 'still used until refresh lands'")
print("[T3a_stale_entry_still_used_meanwhile] STALETICK is marked stale but its stored income "
      "table is untouched (still 2024-12-31) - exactly what a caller would read right now OK")

# select_candidates() must put the stale ticker first (existing Director
# ordering rule, unchanged by this fix) - confirms it's a priority-1
# refetch candidate on the NEXT pre-pass.
_candidates, _ = fis.select_candidates(due_universes=["S&P 500"])
assert _candidates[0] == "STALETICK", _candidates

_new_bundle_no_cf = {"income": _NEW_INCOME, "info": {"currency": "USD"},
                     "meta": {"source": "yfinance"}}
with mock.patch.object(fis.fundamentals_data, "peek_cached_bundle", return_value=_new_bundle_no_cf):
    _refetch_result = fis.run_nightly_prepass(["S&P 500"], log=lambda *a: None, budget=1000)
_refreshed = fis.get("STALETICK")
assert list(_refreshed["income"].columns)[0] == "2025-12-31", _refreshed["income"].columns
assert _refreshed.get("stale") is not True, _refreshed  # save() always clears stale
print(f"[T3b_stale_entry_refetched_by_next_pre_pass] next pre-pass run ({_refetch_result}) "
      "picks STALETICK first (select_candidates() priority-1) and refills it with the newer "
      "(2025-12-31) table, clearing stale OK")


# ======================================================================
# Item 3: run_nightly_prepass()'s own Way 2 gate - don't accept a
# cached income table KNOWN, right there, to already be older than
# that same bundle's own cash-flow statement; accept it when that
# comparison isn't possible (cashflow not cached).
# ======================================================================
_clean()
scan_store.save_scan("S&P 500", _mk_rows([("KNOWNSTALE", 90)]), source_label="fixture")
_bundle_known_stale = {
    "income": _OLD_INCOME,  # latest 2024-12-31
    "cashflow": _mk_cf_df([500.0], [-50.0], ["2025-12-31"]),  # latest 2025-12-31, in the SAME bundle
    "info": {"currency": "USD"}, "meta": {"source": "yfinance"},
}
# budget=0 isolates the decision: if Way 2 wrongly accepts it, outcome is
# "from_store"; if it correctly refuses, Way 3 can't fire either (no
# budget), so outcome is "budget_deferred" and nothing gets saved.
with mock.patch.object(fis.fundamentals_data, "peek_cached_bundle", return_value=_bundle_known_stale):
    _known_stale_result = fis.run_nightly_prepass(["S&P 500"], log=lambda *a: None, budget=0)
assert _known_stale_result["from_store"] == 0, _known_stale_result
assert _known_stale_result["budget_deferred"] == 1, _known_stale_result
assert fis.get("KNOWNSTALE") is None, "a known-stale cached table must never be saved by Way 2"
print(f"[item3a_known_stale_cached_table_rejected] cached bundle's own income (2024-12-31) is "
      f"older than its own cashflow (2025-12-31) - Way 2 refuses it ({_known_stale_result}), "
      "no entry saved, even with zero fetch budget to fall back on OK")

_clean()
scan_store.save_scan("S&P 500", _mk_rows([("UNKNOWNAGE", 90)]), source_label="fixture")
_bundle_unknown_age = {
    "income": _OLD_INCOME, "info": {"currency": "USD"}, "meta": {"source": "yfinance"},
    # no "cashflow" key at all - comparison isn't possible at pre-pass time.
}
with mock.patch.object(fis.fundamentals_data, "peek_cached_bundle", return_value=_bundle_unknown_age):
    _unknown_age_result = fis.run_nightly_prepass(["S&P 500"], log=lambda *a: None, budget=0)
assert _unknown_age_result["from_store"] == 1, _unknown_age_result
_entry_unknown = fis.get("UNKNOWNAGE")
assert _entry_unknown is not None, "when age isn't knowable at pre-pass time, Way 2 must still accept it"
print(f"[item3b_unknown_age_cached_table_accepted] cached bundle has no cashflow to compare "
      f"against - Way 2 accepts the income table anyway ({_unknown_age_result}), leaving "
      "refresh_if_newer()'s own scan-time check to catch it later if it's wrong OK")

_clean()


# ======================================================================
# Item 5: the dry run shows the store entry's own latest period (the
# SAME helper the staleness check itself now uses) and which of the
# three ways filled it.
# ======================================================================
_DRY_INCOME = _mk_income_df([100.0, 95.0], ["2024-12-31", "2023-12-31"])
fis.save("DRYTICK", _DRY_INCOME, currency="USD", source="yfinance_bundle_cached",
         currency_converted=True)
_dry_info = {"sector": "Financial Services", "currency": "USD", "financialCurrency": "USD",
             "sharesOutstanding": 10.0, "currentPrice": 5.0, "marketCap": 50.0}
with mock.patch.object(capm_engine, "get_risk_free_rate", return_value=(0.05, "default")), \
     mock.patch.object(capm_engine, "get_growth_estimates_5y", return_value=(None, "no_coverage")):
    _dry_row = fdr.compute_shadow_row(
        "DRYTICK", "DryCo", 5.0, _dry_info, None, "USD",
        10.0, 50.0, "none", False, "standard", 70,
    )
assert _dry_row.get("store_latest_period") == "2024-12-31", _dry_row.get("store_latest_period")
assert _dry_row.get("store_source") == "yfinance_bundle_cached", _dry_row.get("store_source")
print("[item5_dry_run_latest_period_and_source] compute_shadow_row() now returns "
      f"store_latest_period={_dry_row['store_latest_period']!r} and "
      f"store_source={_dry_row['store_source']!r} - the entry's own data, same helper the "
      "staleness check itself uses, not a separately recorded field OK")
_clean()


print("\nALL ADDENDUM 2 ITEM 6 (STALENESS INDEPENDENT OF SAVE PATH) CHECKS PASSED")
