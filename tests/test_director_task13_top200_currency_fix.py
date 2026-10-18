"""
Director TASK 13 (18 Oct 2026, URGENT, public page): Top 200 must show
every company in its own currency.

Root cause (verified in code): top100_engine.py's pool-selection write
path set each row's "currency" with a shortcut - "AUD" if ticker.
endswith(".AX") else "USD" - written back when the pool was ASX+US
only. Every .L/.TO/European member was therefore stored as "USD"
regardless of its real listing currency (e.g. BCG.L, a London listing,
showed up on the "Top 20 - USA" tab and inflated the "N of 200 priced
in USD" caption).

Fix, two layers:
1. top100_engine.py's write path now uses fcf_valuation_engine.
   trading_currency_for(ticker, info=None) - the same helper
   preview_candidates_by_country() and _cross_listing_traded_value_
   usd() already use for this exact reason. Takes effect at the NEXT
   pool selection.
2. top100_render.py's _enrich_rows() (the single shared choke point
   behind _enriched_pool()/_enriched_asx_extension(), which every tab
   - USA filter, Australia filter, the USD-count caption, and the
   admin dry-run helper - reads from) now RECOMPUTES "currency" via
   the same helper at render time, so the public page is right
   IMMEDIATELY, before the next 23:00 UTC selection ever runs.

Item 3 (every money figure on the page shows its own currency code,
reusing the Scanner's PART 4 money-cell helper): investigated and
confirmed NOT APPLICABLE - top100_render.py renders no price/
intrinsic-value/dollar figure anywhere (cards, the "full dimension
detail" expander, Full 200, filters/tooltips all show Value Score/
MOS%/Research Score/dividend yield/dimension chips/justification text
only; "price"/"intrinsic_value" are read internally by resolver_
engine.dcf_looks_unreliable() for a sanity flag, never displayed).
Nothing to change there - documented here rather than guessed at.

Item 4 (grep the whole Top 200 path for any other .AX shortcut or
hard-coded USD/AUD/$ label): the only other ".AX"-based currency
shortcuts in the codebase are in snapshot_render.py and app.py's own
deep-dive currency logic - genuinely outside the Top 200 path this
task is scoped to, left untouched.

Run: python3 tests/test_director_task13_top200_currency_fix.py
"""
import inspect
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TESTVOL = tempfile.mkdtemp(prefix="task13_top200_currency_test_")
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = TESTVOL

import fcf_valuation_engine
import scan_store
import top100_engine as te
import top100_render as tr
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


_now_dt = datetime.now(timezone.utc)
_FAKE_CADENCE = {"S&P 500": "daily"}


def _row(ticker, long_score, mos=20.0):
    return {
        "Ticker": ticker, "Company Name": f"{ticker} Co", "Quality": 70,
        "MOS %": mos, "Psychology": 5.0, "Discovery (lite)": 10.0,
        "Long Score": long_score, "Price": 50.0, "Intrinsic Value": 55.0,
        "Is Fund": False,
        # A real "Sector" on every synthetic row so select_top100_pool()'s
        # own _fill_missing_sectors() sees nothing missing and never
        # attempts a live yfinance fetch for these made-up tickers -
        # same reasoning as every other fixture-based test in this repo
        # that supplies its own Sector to avoid a real network attempt.
        "Sector": "Industrials",
    }


def _save_universe(universe, rows):
    payload = {
        "universe": universe, "source": "test", "generated_at": _now_dt.isoformat(),
        "run_night": None, "rows": rows, "attention_lite": True, "degraded": False,
    }
    tmp = scan_store._path(universe) + ".tmp"
    with open(tmp, "w") as f:
        json.dump(payload, f)
    os.replace(tmp, scan_store._path(universe))


# ======================================================================
# SECTION A: top100_engine.select_top100_pool() - the write path - sets
# the CORRECT currency for every listing suffix, via trading_currency_
# for(), not the old ".AX"-else-USD shortcut.
# ======================================================================
_rows = [
    _row("BHP.AX", long_score=90.0),    # AUD
    _row("BCG.L", long_score=85.0),     # GBP - the exact regression (was stored as USD)
    _row("SHOP.TO", long_score=80.0),   # CAD
    _row("SAP.DE", long_score=75.0),    # EUR
    _row("VOLV-B.ST", long_score=70.0),  # SEK
    _row("AAPL", long_score=65.0),      # USD (no suffix - genuinely American)
]
_save_universe("S&P 500", _rows)

with mock.patch.object(te.scheduler_engine, "nightly_universe_cadence",
                        return_value=dict(_FAKE_CADENCE)):
    _pool = te.select_top100_pool(log=lambda *a, **k: None)

_pool_by_ticker = {r["ticker"]: r for r in _pool}
check("select_top100_pool() returned all 6 synthetic tickers",
      set(_pool_by_ticker) == {"BHP.AX", "BCG.L", "SHOP.TO", "SAP.DE", "VOLV-B.ST", "AAPL"})

_expected_currency = {
    "BHP.AX": "AUD", "BCG.L": "GBP", "SHOP.TO": "CAD",
    "SAP.DE": "EUR", "VOLV-B.ST": "SEK", "AAPL": "USD",
}
for _ticker, _expected in _expected_currency.items():
    check(f"{_ticker}'s stored currency is {_expected!r} (via "
          "trading_currency_for(), not the old .AX-else-USD guess)",
          _pool_by_ticker[_ticker]["currency"] == _expected)

check("BCG.L (London) is specifically NOT 'USD' - the exact bug this "
      "task fixes (it would show on the Top 20 - USA tab and inflate "
      "the USD-count caption otherwise)",
      _pool_by_ticker["BCG.L"]["currency"] != "USD")

check("every row's stored currency matches calling trading_currency_"
      "for(ticker, info=None) directly - no second, independently-"
      "maintained rule in top100_engine.py",
      all(_pool_by_ticker[t]["currency"] == fcf_valuation_engine.trading_currency_for(t, info=None)
          for t in _pool_by_ticker))

if os.path.exists(scan_store._path("S&P 500")):
    os.remove(scan_store._path("S&P 500"))


# ======================================================================
# SECTION B: top100_render._enrich_rows()/_enriched_pool() - the render
# layer SELF-HEALS a stale stored currency (simulating a pool written
# under the OLD bug, before the next 23:00 UTC selection ever runs).
# ======================================================================
_stale_pool_rows = [
    {"ticker": "BHP.AX", "company_name": "BHP Co", "currency": "AUD",
     "value_score": 90.0, "mos_pct": 20.0, "price": 50.0, "intrinsic_value": 55.0,
     "psychology": 5.0, "sector": "Materials", "dividend_yield_pct": None,
     "most_recent_quarter": None, "generated_at": _now_dt.isoformat(),
     "stale_valuation": False},
    {"ticker": "BCG.L", "company_name": "BCG Co", "currency": "USD",  # the stale/wrong value
     "value_score": 85.0, "mos_pct": 20.0, "price": 50.0, "intrinsic_value": 55.0,
     "psychology": 5.0, "sector": "Industrials", "dividend_yield_pct": None,
     "most_recent_quarter": None, "generated_at": _now_dt.isoformat(),
     "stale_valuation": False},
]

with mock.patch.object(ts, "current_pool", return_value=_stale_pool_rows), \
     mock.patch.object(ts, "latest_scores_for_model", return_value={}), \
     mock.patch.object(ts, "score_failures_for_model", return_value={}):
    _enriched = tr._enriched_pool()

_enriched_by_ticker = {r["ticker"]: r for r in _enriched}
check("_enriched_pool() self-heals BCG.L's stale stored 'USD' to the "
      "real 'GBP' - the public page is right IMMEDIATELY, before any "
      "new selection has run", _enriched_by_ticker["BCG.L"]["currency"] == "GBP")
check("_enriched_pool() leaves BHP.AX's already-correct 'AUD' untouched",
      _enriched_by_ticker["BHP.AX"]["currency"] == "AUD")
check("the stored (stale) pool row itself is never mutated in place - "
      "_enrich_rows() returns NEW dicts, so top100_store.current_pool()'s "
      "own return value is unaffected by this render-time correction",
      _stale_pool_rows[1]["currency"] == "USD")


# ======================================================================
# SECTION C: the 3 render-layer consumers the Director named - USA tab
# filter, Australia tab filter, and the USD-count caption - all read
# from the SELF-HEALED `enriched` rows, confirmed both by replicating
# their exact filter logic here and by a source-text check that the
# real functions read from `enriched`, not the raw, possibly-stale
# `pool`.
# ======================================================================
_usa_tab_rows = [r for r in _enriched if r["currency"] == "USD"]
check("USA tab filter: BCG.L (self-healed to GBP) is correctly EXCLUDED "
      "- it no longer wrongly shows up on 'Top 20 - USA'",
      "BCG.L" not in {r["ticker"] for r in _usa_tab_rows})

_au_tab_rows = [r for r in _enriched if r["currency"] == "AUD"]
check("Australia tab filter: BHP.AX is included, BCG.L is not",
      {r["ticker"] for r in _au_tab_rows} == {"BHP.AX"})

_usd_count_enriched = sum(1 for r in _enriched if r["currency"] == "USD")
_usd_count_raw_stale = sum(1 for r in _stale_pool_rows if r["currency"] == "USD")
check("the corrected USD count (over `enriched`) is LOWER than the "
      "raw stale-pool count would have been - the caption no longer "
      "inflates 'N of 200 priced in USD' with mislabeled .L tickers",
      _usd_count_enriched < _usd_count_raw_stale)
check("the corrected USD count is exactly 0 for this fixture (only "
      "BCG.L was ever mislabeled USD, and it's now correctly GBP)",
      _usd_count_enriched == 0)

_render_src = inspect.getsource(tr.render_top100_page)
check("render_top100_page()'s own usd_count line reads from `enriched` "
      "(the self-healing copy), not the raw `pool`",
      'sum(1 for r in enriched if r["currency"] == "USD")' in _render_src)

_enrich_rows_src = inspect.getsource(tr._enrich_rows)
check("_enrich_rows() itself calls trading_currency_for(row['ticker']) "
      "- confirms the self-healing is wired at the one shared choke "
      "point every tab/consumer reads from",
      'fcf_valuation_engine.trading_currency_for(row["ticker"])' in _enrich_rows_src)


# ======================================================================
# SECTION D: top100_engine.py's write path - source-text confirmation
# that the active code line (not just a comment) now calls trading_
# currency_for(), matching the exact convention _cross_listing_traded_
# value_usd()/preview_candidates_by_country() already use.
# ======================================================================
_select_pool_src = inspect.getsource(te._build_best_by_ticker)
check("_build_best_by_ticker()'s pool-row dict now sets currency via "
      "fcf_valuation_engine.trading_currency_for(ticker, info=None)",
      '"currency": fcf_valuation_engine.trading_currency_for(ticker, info=None)' in _select_pool_src)

print()
print(f"PASS={passed} FAIL={failed}")
sys.exit(1 if failed else 0)
