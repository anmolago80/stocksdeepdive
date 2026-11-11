"""
TASK 35 (Director-directed, owner-reported, 6 Nov 2026): two items.

ITEM A - wire Moat into the Top 200 ranking, behind an owner switch,
with a shadow preview + impact/cost table (goes live only after the
Director reviews the preview on 12 Nov):

FINDING (verified, carried over from TASK 34): moat_engine.MOAT_IN_
VALUE_SCORE is ON in production and Deep Dive's own "Scanner & Top 200
score" subline already used mode="moat_blend" whenever that switch was
on - but top100_engine._recompute_value_score() (what the REAL Top 200
selection/cut-off/previews actually ranks on) never applied moat_blend
at all. BKNG: Deep Dive's subline implied Quality ~85 (moat-blended)
while the stored scan rows (Quality 65/66.5) correctly ranked it below
the Top 200 cut-off under the PLAIN formula.

Fix: a new env var TOP200_MOAT_BLEND_LIVE (re-read per call, same
unset/""/0/off=OFF, 1/on=ON pattern as every other live switch in this
codebase), checked ALONGSIDE moat_engine.MOAT_IN_VALUE_SCORE. The
shared formula itself (ranking_engine.recompute_value_score(row,
moat_blend=bool)) is now the ONE place both top100_engine.py's
selection/cut-off/previews/prescoring AND deep_dive_engine.py's own
"Scanner & Top 200 score" subline compute this from - never two
parallel calculate_long_score() calls again. Default (unset) is
byte-identical to today's plain formula - neither env var is ever set
by this test file.

Shadow preview (SHADOW MODE / TEST ONLY, never affects the live pool):
top100_engine.simulate_pool_moat()/simulate_pool_v2_moat(), the new
"moat"/"value_score_moat"/"value_score_v2_moat" additive fields on
every candidate row, moat_coverage() (A5), and pool_change_impact_and_
cost() (A4, reusing the exact same per-company batch-cost estimate the
nightly "[top100] estimated cost for tonight's batch" line uses).

ITEM B - ELS.AX Fair Value DCF misses its own USD->AUD conversion:

EVIDENCE ([dcf_diag] ELS.AX, 6 Nov 2026): deepdive iv=2.2288 vs
fairvalue iv=1.5335, identical price/growth/discount/shares - the
ratio (1.453) matches the real USD->AUD exchange rate exactly.

ROOT CAUSE (confirmed via fixture, not just inferred): auto_
compounder_engine._run_dcf()/_run_canonical_dcf() both used to
override dcf_info["financialCurrency"] = currency UNCONDITIONALLY
whenever bundle["info"]'s financialCurrency differed from the listing
currency - telling fcf_valuation_engine.dcf_intrinsic_value() "this
bundle's cashflow is already FX-converted, skip your own conversion"
WITHOUT ever verifying that get_bundle() actually ran that conversion
for THIS bundle. A fixture whose cashflow is genuinely still in
financialCurrency (financialCurrency="USD"/currency="AUD", no
"currency_converted" meta flag) reproduces the live symptom exactly:
the blind override suppresses dcf_intrinsic_value()'s own correct
conversion, silently undervaluing the ticker by exactly the real FX
ratio.

Fix: _dcf_info_with_verified_currency_override(bundle, currency) - the
override now only fires when bundle["meta"]["flags"] genuinely
confirms the conversion ran ("currency_converted" - fundamentals_
data.get_bundle()'s own flag). When NOT confirmed, dcf_info is left
unmodified, so dcf_intrinsic_value() converts the (still-unconverted)
cashflow itself - the exact outcome deep_dive_engine.py's own raw
(never pre-converted) cashflow fetch already gets. [dcf_diag] now logs
the real fcf_base (meta["fcf_used"], always set, replacing fcf_base_
used/fcf_base_raw which are usually None) plus fin_ccy/listing_ccy/fx.

Every other bundle-based DCF caller checked (see this task's own
report): admin_data_audit.py's check_a1_before_after()/check_a6_bulk()
and app.py's A6 bulk admin audit panel pass bundle["cashflow"] WITHOUT
any financialCurrency override at all - the OPPOSITE risk (a
DOUBLE-conversion whenever get_bundle() really did convert), confined
to owner-only, read-only, DESIGN-ONLY diagnostic tools that never feed
a customer-facing value - reported, not touched (out of this task's
stated scope: the Fair Value tab's own currency handling).
nightly_scan.py's own resolve_intrinsic_value() call and every
reverse_dcf_engine.compute() call site all use a RAW (never bundle-
sourced) cashflow_df with an unmodified info dict - already correct,
confirmed by inspection, no change needed.

ALL FIXTURES IN THIS FILE ARE SYNTHETIC - this sandbox has no outbound
network access.

Run: python3 tests/test_director_task35_moat_blend_and_dcf_currency.py
"""
import io
import os
import sys
import tempfile
from contextlib import redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TESTVOL = tempfile.mkdtemp(prefix="task35_test_")
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = TESTVOL
os.environ.pop("TOP200_MOAT_BLEND_LIVE", None)
os.environ.pop("MOAT_IN_VALUE_SCORE", None)
os.environ.pop("TOP200_BACKFILL_LIVE", None)
os.environ.pop("PRIVATE_UNIVERSES", None)

import datetime

import pandas as pd

import auto_compounder_engine as ace
import moat_engine
import ranking_engine
import resolver_engine
import scan_store
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
# ITEM A - CHECK 1: ranking_engine.recompute_value_score() - the shared
# formula itself. Plain (moat_blend=False) matches calculate_long_
# score()'s own discovery_measured=False path exactly; moat_blend=True
# matches mode="moat_blend"; a missing "Moat" falls through to the
# existing missing-moat reweighting (never scored as moat=0).
# ======================================================================
_ROW = {"Quality": 65.0, "MOS %": 51.4, "Psychology": 6.96, "Discovery (lite)": 0, "Moat": 86.0}

check("recompute_value_score: moat_blend=False matches calculate_long_score() "
      "directly (discovery_measured=False, no mode override)",
      ranking_engine.recompute_value_score(_ROW, moat_blend=False)
      == ranking_engine.calculate_long_score(65.0, 51.4, 6.96, 0, discovery_measured=False))
check("recompute_value_score: moat_blend=True matches mode='moat_blend' directly",
      ranking_engine.recompute_value_score(_ROW, moat_blend=True)
      == ranking_engine.calculate_long_score(65.0, 51.4, 6.96, 0, mode="moat_blend",
                                              moat_score=86.0, discovery_measured=False))
check("recompute_value_score: the BKNG-shaped fixture (Director's own reported "
      "numbers) reproduces 53.17 under moat_blend",
      ranking_engine.recompute_value_score(_ROW, moat_blend=True) == 53.17)
_row_no_moat = dict(_ROW)
_row_no_moat.pop("Moat")
check("recompute_value_score: a row with no 'Moat' at all, moat_blend=True, falls "
      "through to the missing-moat reweighting (never scored as moat=0 - the "
      "reweighted score differs from what a literal moat_score=0 would give)",
      ranking_engine.recompute_value_score(_row_no_moat, moat_blend=True)
      != ranking_engine.calculate_long_score(65.0, 51.4, 6.96, 0, mode="moat_blend",
                                              moat_score=0.0, discovery_measured=False))
print("[task35_a1_recompute_value_score_shared_formula] ranking_engine."
      "recompute_value_score() matches calculate_long_score() exactly in both modes, "
      "reproduces the BKNG 53.17 figure, and never penalises a missing Moat OK")


# ======================================================================
# ITEM A - CHECK 2: ranking_engine.top200_moat_blend_live() - the env
# switch itself, re-read per call.
# ======================================================================
os.environ.pop("TOP200_MOAT_BLEND_LIVE", None)
check("top200_moat_blend_live: unset -> False", ranking_engine.top200_moat_blend_live() is False)
os.environ["TOP200_MOAT_BLEND_LIVE"] = "1"
check("top200_moat_blend_live: '1' -> True", ranking_engine.top200_moat_blend_live() is True)
os.environ["TOP200_MOAT_BLEND_LIVE"] = "0"
check("top200_moat_blend_live: '0' -> False", ranking_engine.top200_moat_blend_live() is False)
os.environ.pop("TOP200_MOAT_BLEND_LIVE", None)
print("[task35_a2_moat_blend_switch] top200_moat_blend_live() re-reads the env var "
      "correctly every call OK")


# ======================================================================
# ITEM A - CHECK 3: top100_engine._recompute_value_score() - default
# (both switches off, the live production state this test file must
# NEVER change) is byte-identical to the plain formula; moat_blend
# fires ONLY when BOTH TOP200_MOAT_BLEND_LIVE AND moat_engine.MOAT_IN_
# VALUE_SCORE are on.
# ======================================================================
check("_recompute_value_score: both switches off (today's live state) -> plain "
      "formula, byte-identical to calling calculate_long_score() directly",
      te._recompute_value_score(_ROW)
      == ranking_engine.calculate_long_score(65.0, 51.4, 6.96, 0, discovery_measured=False))

os.environ["TOP200_MOAT_BLEND_LIVE"] = "1"
with mock.patch.object(moat_engine, "MOAT_IN_VALUE_SCORE", True):
    check("_recompute_value_score: TOP200_MOAT_BLEND_LIVE=1 AND MOAT_IN_VALUE_SCORE=True "
          "-> moat_blend fires, reproduces 53.17 for the BKNG fixture",
          te._recompute_value_score(_ROW) == 53.17)
with mock.patch.object(moat_engine, "MOAT_IN_VALUE_SCORE", False):
    check("_recompute_value_score: TOP200_MOAT_BLEND_LIVE=1 but MOAT_IN_VALUE_SCORE=False "
          "-> still the plain formula (BOTH switches required)",
          te._recompute_value_score(_ROW)
          == ranking_engine.calculate_long_score(65.0, 51.4, 6.96, 0, discovery_measured=False))
os.environ.pop("TOP200_MOAT_BLEND_LIVE", None)
print("[task35_a3_recompute_value_score_gated] top100_engine._recompute_value_score() "
      "requires BOTH switches on before moat_blend fires; either off keeps today's "
      "plain formula OK")


# ======================================================================
# ITEM A - CHECK 4: live selection is byte-identical whether a scan row
# carries a "Moat" score or not, with the switch OFF (the Director's
# own explicit test requirement, mirroring TASK 32's Check 8) - plus
# the additive "moat"/"value_score_moat"/"value_score_v2_moat" fields
# themselves (which ARE supposed to differ) are excluded from that
# comparison.
# ======================================================================
def _scan_row(ticker, quality=60.0, moat=50.0, quality_v2=None, mos=20.0, psychology=5.0,
              discovery=30.0):
    row = {
        "Ticker": ticker, "Company Name": f"{ticker} Co", "Price": 100.0,
        "Intrinsic Value": 120.0, "MOS %": mos, "Long Score": 60.0,
        "Quality": quality, "Psychology": psychology, "Discovery (lite)": discovery,
        "Moat": moat, "Growth Used": 0.05, "FCF Source": "reported",
        "Dividend Yield %": 2.0, "Is Fund": False, "price_unit_suspect": False,
        "Trading Status": None, "DCF Unreliable": False, "Sector": "Industrials",
    }
    if quality_v2 is not None:
        row["Quality V2"] = quality_v2
        row["Quality V2 Meta"] = {"fiscal_years_used": [], "fallback_fields": [], "defaulted": False}
    return row


if os.path.exists(ts.DB_PATH):
    os.remove(ts.DB_PATH)
_rows_no_moat = [_scan_row(f"SEL{i}", quality=50.0 + i, moat=None) for i in range(10)]
for _r in _rows_no_moat:
    _r.pop("Moat")
scan_store.save_scan("S&P 500", _rows_no_moat, source_label="test fixture")
_pool_without_moat = te.select_top100_pool(log=lambda *a, **k: None)

if os.path.exists(ts.DB_PATH):
    os.remove(ts.DB_PATH)
_rows_with_moat = [_scan_row(f"SEL{i}", quality=50.0 + i, moat=90.0 - i) for i in range(10)]
scan_store.save_scan("S&P 500", _rows_with_moat, source_label="test fixture")
_pool_with_moat = te.select_top100_pool(log=lambda *a, **k: None)

_NON_LIVE_FIELDS = (
    "generated_at", "source_universe_generated_at",
    "moat", "value_score_moat", "value_score_v2_moat",
    # TASK 42 item 6a (8 Nov 2026, Director-directed): same additive-
    # shadow-field status as value_score_v2_moat just above - it falls
    # back to that exact field whenever Fear/Greed/Fomo or a drop_chip_
    # store peak entry is missing (both true in this fixture), so it
    # inherits the SAME "depends on Moat, supposed to differ" reason.
    "value_score_v2_moat_peak_fear",
)


def _strip_non_live_fields(pool):
    return [{k: v for k, v in row.items() if k not in _NON_LIVE_FIELDS} for row in pool]


check("select_top100_pool(): LIVE fields (ticker order, value_score, every other "
      "live field) are BYTE-IDENTICAL whether scan rows carry a Moat score or not, "
      "with TOP200_MOAT_BLEND_LIVE off - the live selection never reads Moat yet",
      _strip_non_live_fields(_pool_without_moat) == _strip_non_live_fields(_pool_with_moat))
print("[task35_a4_live_selection_byte_identical] select_top100_pool() is completely "
      "unaffected by Moat's presence/absence while the switch is off OK")


# ======================================================================
# ITEM A - CHECK 5: with the switch ON, ranking uses moat_blend - a
# ticker with low live Quality but a high Moat score ENTERS the pool
# under moat_blend; one with high Quality but low Moat DROPS OUT.
# ======================================================================
if os.path.exists(ts.DB_PATH):
    os.remove(ts.DB_PATH)
# Quality held CONSTANT at 50 (matching every PAD row) for both swing
# tickers - only Moat varies - so the swing isolates moat_blend's own
# Moat term (weight 0.15 pre-reweight, materially smaller than
# Quality's 0.25) rather than being swamped by a simultaneous Quality
# swing the way a naive 5-vs-95 Quality change would.
_moat_sim_rows = [_scan_row(f"PAD{i}", quality=50.0, moat=50.0) for i in range(199)]
_moat_sim_rows.append(_scan_row("HIGH_MOAT", quality=50.0, moat=100.0))
_moat_sim_rows.append(_scan_row("LOW_MOAT", quality=50.0, moat=0.0))
scan_store.save_scan("S&P 500", _moat_sim_rows, source_label="test fixture")

os.environ["TOP200_MOAT_BLEND_LIVE"] = "1"
with mock.patch.object(moat_engine, "MOAT_IN_VALUE_SCORE", True):
    _moat_pool = te.select_top100_pool(log=lambda *a, **k: None)
os.environ.pop("TOP200_MOAT_BLEND_LIVE", None)
_moat_pool_tickers = {r["ticker"] for r in _moat_pool}
check("switch ON: HIGH_MOAT (low live Quality, high Moat) enters the live-selection "
      "pool once moat_blend is actually ranking on it",
      "HIGH_MOAT" in _moat_pool_tickers)
check("switch ON: LOW_MOAT (high live Quality, zero Moat) is NOT in the pool",
      "LOW_MOAT" not in _moat_pool_tickers)
print("[task35_a5_moat_blend_live_selection] with TOP200_MOAT_BLEND_LIVE on, the Top "
      "200's own live selection actually ranks on moat_blend OK")


# ======================================================================
# ITEM A - CHECK 6: _build_best_by_ticker()'s additive "moat"/
# "value_score_moat"/"value_score_v2_moat" fields - never read by the
# live sort/slice ("value_score" only), always computed regardless of
# either switch's state (SHADOW, not gated).
# ======================================================================
if os.path.exists(ts.DB_PATH):
    os.remove(ts.DB_PATH)
scan_store.save_scan(
    "S&P 500", [_scan_row("AAA35", quality=60.0, moat=86.0, quality_v2=80.0)],
    source_label="test fixture",
)
_best35, _ = te._build_best_by_ticker(log=lambda *a, **k: None)
_row35 = _best35["AAA35"]
check("additive field: 'moat' == the stored Moat score (86.0)", _row35["moat"] == 86.0)
check("additive field: 'value_score_moat' == recompute_value_score(row, moat_blend=True) "
      "on the LIVE Quality",
      _row35["value_score_moat"] == ranking_engine.recompute_value_score(
          {"Quality": 60.0, "MOS %": 20.0, "Psychology": 5.0, "Discovery (lite)": 30.0, "Moat": 86.0},
          moat_blend=True))
check("additive field: 'value_score_v2_moat' == recompute_value_score(row, moat_blend=True) "
      "with Quality V2 (80.0) swapped in for Quality",
      _row35["value_score_v2_moat"] == ranking_engine.recompute_value_score(
          {"Quality": 80.0, "MOS %": 20.0, "Psychology": 5.0, "Discovery (lite)": 30.0, "Moat": 86.0},
          moat_blend=True))
check("additive field: 'value_score' (live) is COMPLETELY UNCHANGED by any of this",
      _row35["value_score"] == ranking_engine.calculate_long_score(
          60.0, 20.0, 5.0, 30.0, discovery_measured=False))
print("[task35_a6_additive_fields] moat/value_score_moat/value_score_v2_moat are "
      "purely additive shadow fields - 'value_score' itself is untouched OK")

if os.path.exists(ts.DB_PATH):
    os.remove(ts.DB_PATH)
scan_store.save_scan("S&P 500", [_scan_row("BBB35", quality=55.0, quality_v2=None)],
                      source_label="test fixture")
_best35b, _ = te._build_best_by_ticker(log=lambda *a, **k: None)
# TASK 40 (Director-directed, 8 Nov 2026, review finding): no Quality
# V2 -> 'value_score_v2_moat' now falls back to the live Quality
# blended with Moat (matching 'value_score_moat' exactly) instead of
# None - see _recompute_value_score_v2_moat()'s own docstring.
check("no Quality V2 on this row -> 'value_score_v2_moat' falls back to "
      "'value_score_moat' (same fallback as 'value_score_v2' itself)",
      _best35b["BBB35"]["value_score_v2_moat"] == _best35b["BBB35"]["value_score_moat"])
print("[task35_a6b_v2_moat_falls_back] value_score_v2_moat falls back to value_score_moat "
      "whenever Quality V2 itself is missing, never a crash, never hidden as None OK")


# ======================================================================
# ITEM A - CHECK 7: simulate_pool_moat()/simulate_pool_v2_moat() - a
# ticker with a much higher Moat enters; one with a much lower Moat
# drops out. Read-only: select_top100_pool()'s own live output is
# untouched by calling either.
# ======================================================================
if os.path.exists(ts.DB_PATH):
    os.remove(ts.DB_PATH)
_sim_moat_rows = [_scan_row(f"PAD2{i}", quality=50.0, moat=50.0) for i in range(199)]
_sim_moat_rows.append(_scan_row("HIGH_MOAT2", quality=50.0, moat=100.0))
_sim_moat_rows.append(_scan_row("LOW_MOAT2", quality=50.0, moat=0.0))
_sim_moat_rows.append(_scan_row("AU_HIGH_MOAT.AX", quality=50.0, moat=100.0))
scan_store.save_scan("S&P 500", _sim_moat_rows, source_label="test fixture")

_pool_before = te.select_top100_pool(log=lambda *a, **k: None)
_sim_moat = te.simulate_pool_moat(log=lambda *a, **k: None)
_sim_v2_moat = te.simulate_pool_v2_moat(log=lambda *a, **k: None)
_pool_after = te.select_top100_pool(log=lambda *a, **k: None)

check("simulate_pool_moat: HIGH_MOAT2 (low Quality, high Moat) enters the simulated pool",
      "HIGH_MOAT2" in _sim_moat["pool_tickers"])
check("simulate_pool_moat: LOW_MOAT2 (high Quality, zero Moat) is NOT in the simulated pool",
      "LOW_MOAT2" not in _sim_moat["pool_tickers"])
check("simulate_pool_moat: AU_HIGH_MOAT.AX enters the simulated pool or AU extension",
      "AU_HIGH_MOAT.AX" in _sim_moat["pool_tickers"] | _sim_moat["extension_tickers"])
check("simulate_pool_moat: cutoff is a real number", isinstance(_sim_moat["cutoff"], float))
check("simulate_pool_v2_moat: returns the same generic shape (pool_tickers/cutoff)",
      isinstance(_sim_v2_moat["pool_tickers"], set) and "cutoff" in _sim_v2_moat)
check("simulate_pool_moat/simulate_pool_v2_moat are READ-ONLY - select_top100_pool()'s "
      "own live output is byte-identical before and after calling both",
      _pool_before == _pool_after)
print("[task35_a7_simulate_pool_moat] simulate_pool_moat()/simulate_pool_v2_moat() "
      "correctly swap a low-quality/high-moat ticker in, a high-quality/zero-moat "
      "ticker out, and never touch the live selection OK")


# ======================================================================
# ITEM A - CHECK 8 (A5): moat_coverage() - reports with_moat/total/pct
# overall and per universe; a row with Moat=None is counted as NOT
# covered, never crashes.
# ======================================================================
if os.path.exists(ts.DB_PATH):
    os.remove(ts.DB_PATH)
_cov_rows = [_scan_row(f"COV{i}", moat=70.0) for i in range(7)]
for _r in _cov_rows[:3]:
    _r.pop("Moat")
    _r["Moat"] = None
scan_store.save_scan("S&P 500", _cov_rows, source_label="test fixture")
_cov = te.moat_coverage(log=lambda *a, **k: None)
check("moat_coverage: overall with_moat=4, total=7 (3 rows have Moat=None)",
      _cov["overall"]["with_moat"] == 4 and _cov["overall"]["total"] == 7)
check("moat_coverage: overall pct == 4/7*100", abs(_cov["overall"]["pct"] - (4 / 7 * 100.0)) < 1e-9)
check("moat_coverage: per-universe breakdown includes 'S&P 500' with the same counts",
      _cov["by_universe"].get("S&P 500", {}).get("with_moat") == 4
      and _cov["by_universe"].get("S&P 500", {}).get("total") == 7)
print("[task35_a8_moat_coverage] moat_coverage() correctly counts rows with/without "
      "a stored Moat score, overall and per universe OK")


# ======================================================================
# ITEM A - CHECK 9 (A4): pool_change_impact_and_cost() - entrants/
# drop-outs counted correctly, entrants with no stored Claude research
# score flagged, estimated cost computed via the SAME batch-cost
# estimate the nightly log line uses (never zero for a real entrant
# list, never crashing when top100_store has no scores at all yet).
# ======================================================================
with mock.patch.object(ts, "current_pool", return_value=[{"ticker": "LOW_MOAT2"}]), \
     mock.patch.object(ts, "current_asx_extension", return_value=[]):
    _impact = te.pool_change_impact_and_cost(_sim_moat, log=lambda *a, **k: None)
check("pool_change_impact_and_cost: dropouts counts LOW_MOAT2 (in the mocked live "
      "pool, not in the simulated one)", _impact["dropouts"] == 1)
check("pool_change_impact_and_cost: entrants counts every simulated-pool ticker not "
      "in the mocked live pool (at least HIGH_MOAT2)", _impact["entrants"] >= 1)
check("pool_change_impact_and_cost: changed_pct_of_200 == changed_count / 200 * 100",
      abs(_impact["changed_pct_of_200"] - (_impact["changed_count"] / 200.0 * 100.0)) < 1e-9)
check("pool_change_impact_and_cost: every entrant has no stored Claude score yet "
      "(a fresh test DB) - all of them show up in entrants_unscored",
      set(_impact["entrants_unscored"]) == (_sim_moat["pool_tickers"] - {"LOW_MOAT2"}))
check("pool_change_impact_and_cost: estimated_scoring_cost_usd is a positive number "
      "for a non-empty unscored-entrants list",
      _impact["estimated_scoring_cost_usd"] > 0)
print("[task35_a9_pool_change_impact_and_cost] pool_change_impact_and_cost() counts "
      "entrants/dropouts correctly, flags every unscored entrant, and produces a "
      "positive cost estimate via the shared batch-cost estimator OK")


# ======================================================================
# ITEM B - CHECK 1: _dcf_info_with_verified_currency_override() - the
# shared helper itself.
# ======================================================================
_B_INFO = {"financialCurrency": "USD", "currency": "AUD"}
_bundle_converted = {"info": _B_INFO, "meta": {"flags": ["currency_converted"]}}
_bundle_not_converted = {"info": _B_INFO, "meta": {"flags": []}}
_bundle_same_ccy = {"info": {"financialCurrency": "USD", "currency": "USD"}, "meta": {"flags": []}}

_info1, _conv1 = ace._dcf_info_with_verified_currency_override(_bundle_converted, "AUD")
check("verified-converted bundle: financialCurrency IS overridden to the listing "
      "currency (confirmed via the meta flag)",
      _info1.get("financialCurrency") == "AUD" and _conv1 is True)

_info2, _conv2 = ace._dcf_info_with_verified_currency_override(_bundle_not_converted, "AUD")
check("NOT-verified-converted bundle (the ELS.AX symptom's exact shape): financialCurrency "
      "is left UNMODIFIED, never blindly overridden",
      _info2.get("financialCurrency") == "USD" and _conv2 is False)

_info3, _conv3 = ace._dcf_info_with_verified_currency_override(_bundle_same_ccy, "USD")
check("same-currency bundle: no override needed either way, info unmodified",
      _info3.get("financialCurrency") == "USD" and _conv3 is False)
print("[task35_b1_verified_currency_override_helper] _dcf_info_with_verified_currency_"
      "override() only overrides financialCurrency when the bundle's own meta "
      "genuinely confirms the conversion ran OK")


# ======================================================================
# ITEM B - CHECK 2: ELS.AX-shaped fixture (reports in USD, trades in
# AUD, bundle cashflow genuinely NOT pre-converted - no "currency_
# converted" flag) - the Fair Value path must now match the Deep Dive
# path exactly (previously off by exactly the real USD->AUD rate).
# ======================================================================
_ELS_YEARS = ["2026-12-31", "2025-12-31", "2024-12-31", "2023-12-31", "2022-12-31"]
_ELS_INFO = {
    "currentPrice": 1.43, "currency": "AUD", "financialCurrency": "USD",
    "marketCap": 1.43 * 226_033_984, "sharesOutstanding": 226_033_984,
}
_ELS_RAW_USD_CASHFLOW = pd.DataFrame(
    {y: [0.08, -0.01] for y in _ELS_YEARS}, index=["Operating Cash Flow", "Capital Expenditure"],
)

_els_deepdive_result = resolver_engine.resolve_intrinsic_value(
    "ELSTEST35", None, info=dict(_ELS_INFO), cashflow_df=_ELS_RAW_USD_CASHFLOW, currency="AUD",
    discount_rate=0.1127, perpetual_rate=0.025, growth_rate=0.20,
)
_els_dd_value, _els_dd_src, _els_dd_growth, _els_dd_meta = _els_deepdive_result

_els_bundle = {
    "info": dict(_ELS_INFO), "cashflow": _ELS_RAW_USD_CASHFLOW, "income": None,
    "meta": {"flags": [], "fetched_at": datetime.datetime.now(datetime.timezone.utc).isoformat()},
}
_els_log = io.StringIO()
with redirect_stdout(_els_log):
    _els_fv_result = ace._run_canonical_dcf(_els_bundle, "ELSTEST35", discount_rate=0.1127,
                                             perpetual_rate=0.025, growth_rate=0.20)
check("ELS.AX-shaped fixture: Fair Value DCF now matches the Deep Dive DCF EXACTLY "
      "(previously off by the real USD->AUD fx ratio)",
      _els_fv_result is not None and abs(_els_fv_result["value"] - _els_dd_value) < 1e-9)
_els_log_text = _els_log.getvalue()
check("[dcf_diag] now logs fin_ccy/listing_ccy/fx and the real fcf_base (meta['fcf_used'], "
      "always set) - not the old fcf_base_used/fcf_base_raw (usually None)",
      "fin_ccy=USD" in _els_log_text and "listing_ccy=AUD" in _els_log_text
      and "fx=" in _els_log_text and f"fcf_base={_els_dd_meta.get('fcf_used')}" in _els_log_text)
print(f"[task35_b2_els_fixture_fixed] ELS.AX-shaped fixture: fairvalue={_els_fv_result['value']:.6g} "
      f"== deepdive={_els_dd_value:.6g} exactly, [dcf_diag] now names fin_ccy/listing_ccy/fx OK")


# ======================================================================
# ITEM B - CHECK 3: CSL.AX-shaped fixture (genuinely pre-converted
# bundle, WITH the "currency_converted" flag correctly set) - the fix
# must NOT re-introduce a double-conversion regression.
# ======================================================================
import fcf_valuation_engine as fve

_CSL_INFO_35 = {
    "currentPrice": 300.0, "currency": "AUD", "financialCurrency": "USD",
    "marketCap": 300.0 * 500_000_000, "sharesOutstanding": 500_000_000,
}
_csl_raw_cashflow = pd.DataFrame(
    {y: [10.0, -1.0] for y in _ELS_YEARS}, index=["Operating Cash Flow", "Capital Expenditure"],
)
_fx_usd_aud, _ = fve.fx_rate("USD", "AUD")
_csl_converted_cashflow = _csl_raw_cashflow * _fx_usd_aud

_csl_dd_value, _, _, _ = resolver_engine.resolve_intrinsic_value(
    "CSLTEST35", None, info=dict(_CSL_INFO_35), cashflow_df=_csl_raw_cashflow, currency="AUD",
    discount_rate=0.09, perpetual_rate=0.025, growth_rate=0.08,
)
_csl_bundle_35 = {
    "info": dict(_CSL_INFO_35), "cashflow": _csl_converted_cashflow, "income": None,
    "meta": {"flags": ["currency_converted"]},
}
_csl_fv_result = ace._run_canonical_dcf(_csl_bundle_35, "CSLTEST35", discount_rate=0.09,
                                         perpetual_rate=0.025, growth_rate=0.08)
check("CSL.AX-shaped fixture (genuinely pre-converted, flag correctly set): Fair "
      "Value DCF still matches Deep Dive exactly - NO double-conversion regression",
      _csl_fv_result is not None and abs(_csl_fv_result["value"] - _csl_dd_value) < 1e-9)
print("[task35_b3_csl_no_regression] a genuinely pre-converted bundle (the normal, "
      "common case) is NOT affected by this fix - no double-conversion introduced OK")


# ======================================================================
# ITEM B - CHECK 4: a same-currency ticker (no financialCurrency
# mismatch at all) is completely unaffected, regardless of the meta
# flag's presence.
# ======================================================================
_SAME_CCY_INFO = {
    "currentPrice": 50.0, "currency": "USD", "financialCurrency": "USD",
    "marketCap": 50.0 * 10_000_000, "sharesOutstanding": 10_000_000,
}
_same_ccy_cashflow = pd.DataFrame(
    {y: [5.0, -0.5] for y in _ELS_YEARS}, index=["Operating Cash Flow", "Capital Expenditure"],
)
_same_ccy_bundle_no_flag = {"info": dict(_SAME_CCY_INFO), "cashflow": _same_ccy_cashflow,
                            "income": None, "meta": {"flags": []}}
_same_ccy_bundle_flag = {"info": dict(_SAME_CCY_INFO), "cashflow": _same_ccy_cashflow,
                         "income": None, "meta": {"flags": ["currency_converted"]}}
_v1 = ace._run_canonical_dcf(_same_ccy_bundle_no_flag, "SAMECCY35", discount_rate=0.09,
                              perpetual_rate=0.02, growth_rate=0.06)
_v2 = ace._run_canonical_dcf(_same_ccy_bundle_flag, "SAMECCY35", discount_rate=0.09,
                              perpetual_rate=0.02, growth_rate=0.06)
check("same-currency ticker: value is IDENTICAL regardless of the meta flag - "
      "nothing about this fix changes a same-currency ticker's DCF at all",
      _v1 is not None and _v2 is not None and _v1["value"] == _v2["value"])
print("[task35_b4_same_currency_unaffected] a same-currency ticker's DCF is "
      "byte-identical regardless of the currency_converted flag OK")


print()
print(f"PASS={passed} FAIL={failed}")
sys.exit(1 if failed else 0)
