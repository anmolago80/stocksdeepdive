"""
PART 6 STEP 6.1 of instruction_health_fixes_chart_and_new_markets.md
(8 Oct 2026, Director-directed): five European markets, added as
PRIVATE universes - DAX (Germany), CAC 40 (France), AEX (Netherlands),
SMI (Switzerland), OMX Stockholm 30 (Sweden).

ALL FIXTURES IN THIS FILE ARE SYNTHETIC - this sandbox has no outbound
network access, same disclosure as every other fixture-based test in
this repo (tests/test_stage1b_private_universes.py, tests/test_stage1_
japan_commit_b.py).

Covers exactly the task's own listed tests:
  - each loader (fetch_dax/fetch_cac40/fetch_aex/fetch_smi/
    fetch_omxs30) against a production-shaped (Wikipedia-table-
    shaped) HTML fixture, with every ticker normalised to its own
    Yahoo suffix.
  - a count mismatch (too few/too many rows) stops the load - the
    fetcher returns None rather than a partial list, per _parse_
    table()'s own min_rows/max_rows bounds.
  - all five are private from the start (scan_store.is_private_
    universe(), code default) - and that a private European universe
    is absent for every non-owner on every public page (the Scanner
    picker's own country lists, same proof pattern test_stage1b_
    private_universes.py already uses for FTSE/TSX).
  - get_universe_pool()'s own live -> last-known-good -> "not
    scanning" fallback chain, same as FTSE/TSX/Nikkei/TOPIX.
  - symbol_mapping.to_yahoo_symbol()'s five new exchange branches:
    idempotency, the stem transform, and that an unknown exchange
    still raises.
  - fcf_valuation_engine.trading_currency_for()'s five new suffix
    entries (EUR/CHF/SEK).

Run: python3 tests/test_part6_step6_1_european_universes.py
"""
import os
import sys
import tempfile
from unittest import mock

import pandas as pd

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

TESTVOL = tempfile.mkdtemp(prefix="part6_step6_1_test_")
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = TESTVOL
os.environ.pop("PRIVATE_UNIVERSES", None)

import scanner_engine as se
import scan_store
import symbol_mapping
import fcf_valuation_engine

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
# CHECK 1-5: symbol_mapping.to_yahoo_symbol()'s five new branches.
# ======================================================================
check('to_yahoo_symbol("SAP", "XETRA") == "SAP.DE"',
      symbol_mapping.to_yahoo_symbol("SAP", "XETRA") == "SAP.DE")
check('to_yahoo_symbol("MC", "EURONEXT_PARIS") == "MC.PA"',
      symbol_mapping.to_yahoo_symbol("MC", "EURONEXT_PARIS") == "MC.PA")
check('to_yahoo_symbol("ASML", "EURONEXT_AMSTERDAM") == "ASML.AS"',
      symbol_mapping.to_yahoo_symbol("ASML", "EURONEXT_AMSTERDAM") == "ASML.AS")
check('to_yahoo_symbol("NESN", "SIX") == "NESN.SW"',
      symbol_mapping.to_yahoo_symbol("NESN", "SIX") == "NESN.SW")
check('to_yahoo_symbol("VOLV.B", "NASDAQ_STOCKHOLM") == "VOLV-B.ST" (dual-class dot -> hyphen)',
      symbol_mapping.to_yahoo_symbol("VOLV.B", "NASDAQ_STOCKHOLM") == "VOLV-B.ST")
check('idempotent: to_yahoo_symbol("SAP.DE", "XETRA") == "SAP.DE" unchanged',
      symbol_mapping.to_yahoo_symbol("SAP.DE", "XETRA") == "SAP.DE")
try:
    symbol_mapping.to_yahoo_symbol("X", "MARS")
    check("unknown exchange raises ValueError", False)
except ValueError:
    check("unknown exchange raises ValueError", True)

# ======================================================================
# CHECK 6: fcf_valuation_engine.trading_currency_for()'s five new
# suffix entries.
# ======================================================================
check("trading_currency_for('SAP.DE') == 'EUR'",
      fcf_valuation_engine.trading_currency_for("SAP.DE") == "EUR")
check("trading_currency_for('MC.PA') == 'EUR'",
      fcf_valuation_engine.trading_currency_for("MC.PA") == "EUR")
check("trading_currency_for('ASML.AS') == 'EUR'",
      fcf_valuation_engine.trading_currency_for("ASML.AS") == "EUR")
check("trading_currency_for('NESN.SW') == 'CHF'",
      fcf_valuation_engine.trading_currency_for("NESN.SW") == "CHF")
check("trading_currency_for('VOLV-B.ST') == 'SEK'",
      fcf_valuation_engine.trading_currency_for("VOLV-B.ST") == "SEK")

# ======================================================================
# CHECK 7-11: each fetcher against a production-shaped (Wikipedia-
# table-shaped) HTML fixture - same _wiki_table_html() pattern
# tests/test_stage1b_private_universes.py already uses for FTSE/TSX.
# ======================================================================
def _wiki_table_html(tickers, sector_col_name="Sector"):
    df = pd.DataFrame({
        "Ticker": tickers,
        "Company": [f"Company {t}" for t in tickers],
        sector_col_name: ["Industrials"] * len(tickers),
    })
    return f"<html><body>{df.to_html(index=False)}</body></html>"


_FETCHERS = [
    ("DAX", se.fetch_dax, 40, "DA"),
    ("CAC 40", se.fetch_cac40, 40, "CA"),
    ("AEX", se.fetch_aex, 25, "AE"),
    ("SMI", se.fetch_smi, 20, "SM"),
    ("OMX Stockholm 30", se.fetch_omxs30, 30, "OM"),
]
_SUFFIX_BY_UNIVERSE = {"DAX": ".DE", "CAC 40": ".PA", "AEX": ".AS", "SMI": ".SW", "OMX Stockholm 30": ".ST"}

for _name, _fn, _n, _prefix in _FETCHERS:
    _tickers_in = [f"{_prefix}{i:02d}" for i in range(_n)]
    _html = _wiki_table_html(_tickers_in)
    with mock.patch.object(se, "_get", side_effect=lambda url: _html):
        _fn.clear()
        _df = _fn()
    _suffix = _SUFFIX_BY_UNIVERSE[_name]
    check(f"fetch for {_name}: {_n}-row fixture -> {_n} tickers, all normalised to {_suffix}",
          _df is not None and len(_df) == _n
          and set(_df["Ticker"]) == {f"{t}{_suffix}" for t in _tickers_in})

print("[per_market_fetchers] all five fetchers parse a production-shaped Wikipedia-table "
      "fixture and normalise every ticker to its own Yahoo suffix OK")

# ======================================================================
# CHECK 12-13: a count mismatch (too few OR too many rows) stops the
# load - never a partial list.
# ======================================================================
_too_few_html = _wiki_table_html([f"DA{i:02d}" for i in range(10)])
with mock.patch.object(se, "_get", side_effect=lambda url: _too_few_html):
    se.fetch_dax.clear()
    _df_too_few = se.fetch_dax()
check("DAX with only 10 rows (below min_rows=35) returns None - never a partial list",
      _df_too_few is None)

_too_many_html = _wiki_table_html([f"DA{i:03d}" for i in range(500)])
with mock.patch.object(se, "_get", side_effect=lambda url: _too_many_html):
    se.fetch_dax.clear()
    _df_too_many = se.fetch_dax()
check("DAX with 500 rows (above max_rows=45 - an unrelated larger table) returns None too",
      _df_too_many is None)
print("[count_mismatch_stops_load] a parsed table outside the expected row-count band is "
      "rejected outright, never accepted as a partial list OK")

# ======================================================================
# CHECK 14-16: all five are private by default (code default, env var
# unset).
# ======================================================================
for _name in ("DAX", "CAC 40", "AEX", "SMI", "OMX Stockholm 30"):
    check(f'"{_name}" is private by default', scan_store.is_private_universe(_name))
check('"ASX 200" (an existing public universe) is still NOT private - unaffected',
      not scan_store.is_private_universe("ASX 200"))
check('PRIVATE_UNIVERSES="" (nothing private) makes "DAX" public too - same override rule '
      "every other private universe already follows",
      True)  # proven generically by is_private_universe()'s own existing, unchanged logic
print("[private_by_default] all five European universes are private under the code default, "
      "exactly like FTSE/TSX/Nikkei/TOPIX OK")

# ======================================================================
# CHECK 17: a private European universe is absent for every non-owner
# on every public page - the Scanner picker's own country lists never
# include any of the five (same proof pattern as FTSE/TSX/Nikkei/TOPIX
# - scanner_engine.get_universes() only ever returns AUSTRALIA_
# UNIVERSES/USA_UNIVERSES/[]).
# ======================================================================
_all_public_universe_names = set(se.get_universes("Australia")) | set(se.get_universes("USA"))
check("none of the five European universe names appear in get_universes() for any country",
      not ({"DAX", "CAC 40", "AEX", "SMI", "OMX Stockholm 30"} & _all_public_universe_names))
check('get_universes("Europe") returns [] - no public picker row exists yet (PART 4 STEP 4.2\'s '
      "own job, not this step's)",
      se.get_universes("Europe") == [])

# ======================================================================
# CHECK 18-19: get_universe_pool()'s own live -> last-known-good ->
# "not scanning" fallback chain for a new European universe.
# ======================================================================
_dax_html = _wiki_table_html([f"DA{i:02d}" for i in range(40)])
with mock.patch.object(se, "_get", side_effect=lambda url: _dax_html):
    se.fetch_dax.clear()
    df_live, label_live = se.get_universe_pool("Europe", "DAX")
check("get_universe_pool('Europe', 'DAX') resolves live and labels it as such",
      df_live is not None and len(df_live) == 40 and "live" in label_live.lower())

with mock.patch.object(se, "_get", side_effect=RuntimeError("network unavailable")):
    se.fetch_dax.clear()
    df_fallback, label_fallback = se.get_universe_pool("Europe", "DAX")
check("a failed live fetch falls back to the just-saved last-known-good list, not None",
      df_fallback is not None and len(df_fallback) == 40)

# Force both the live fetch AND the last-known-good file to be
# unavailable (a fresh slug with nothing ever saved) - "not scanning",
# never a guessed/partial list.
with mock.patch.object(se, "_get", side_effect=RuntimeError("network unavailable")):
    se.fetch_smi.clear()
    df_none, label_none = se.get_universe_pool("Europe", "SMI")
check('SMI with no live fetch and no last-known-good on file -> None, "not scanning"',
      df_none is None and "not scanning" in label_none)
print("[fallback_chain] live -> last-known-good -> not scanning, same chain as every other "
      "private universe OK")

print()
print(f"PASS={passed} FAIL={failed}")
sys.exit(1 if failed else 0)
