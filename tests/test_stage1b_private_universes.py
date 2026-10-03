"""
Stage 1b - FTSE and TSX universes, scanned privately (Director-directed,
3 Oct 2026).

Covers:
  T1: fetch_ftse100()/fetch_ftse250()/fetch_tsx60()/fetch_tsxcomposite()
      against saved Wikipedia-shaped HTML FIXTURES (synthetic - this
      sandbox has no live network access, same disclosure as every
      other fixture-based test in this repo).
  T2: verify_universe_before_save()'s two new special-case branches -
      FTSE 250 must not overlap FTSE 100; TSX 60 must be a strict
      subset of TSX Composite - plus UNIVERSE_INTEGRITY_TRACKED_
      UNIVERSES membership.
  T3: scan_store privacy primitives (is_private_universe/list_saved_
      universes/load_scan/load_scan_raw/find_ticker_row) - default DENY
      vs PRIVATE_UNIVERSES="".
  T4: byte-identical selection - top100_engine.select_top100_pool()'s
      output with the 4 private universes' scan files present (default
      PRIVATE_UNIVERSES) is IDENTICAL to a run where those files don't
      exist at all; with PRIVATE_UNIVERSES="" the private rows DO
      appear.
  T5: a ticker saved under both a private and a public universe keeps
      its public row (find_ticker_row's default-deny behaviour).
  T6: price_unit_suspect -> no Intrinsic Value/MOS, with a reason, in
      BOTH deep_dive_engine.analyze()'s headline and nightly_scan.
      analyze_ticker_lite()'s scan row.
  T7: scheduler_engine._DEFAULT_NIGHTLY_UNIVERSES before/after schedule
      table - every pre-existing entry's cadence is unchanged; the 4
      new entries land on sun/tue as specified.
  T8: scheduler_engine._run_nightly()'s per-universe loop skips the
      public snapshot/alert side effects for a private universe (but
      still runs insider_engine.refresh_universe(), deliberately left
      ungated).
  T9: "real" before/after (not a recomputed-from-constants argument) -
      the 7 fixtures from the Director Q&A answer (5 existing + 2 new
      bank fixtures) run against the REAL code at commit de575e6 (the
      Stage 1a-fix commit, immediately before this Stage 1b task) and
      against this commit's current code - discount rate/growth
      ceiling/growth end-rate must MATCH, since Stage 1b's only changes
      to this code path are the GBP/CAD FX constants (inert for a USD/
      AUD ticker) and the price_unit_suspect carry-over (inert for a
      non-suspect ticker).

Run: python3 tests/test_stage1b_private_universes.py
"""
import contextlib
import importlib.util
import io
import os
import subprocess
import sys
import tempfile
from unittest import mock

import pandas as pd

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

TESTVOL = tempfile.mkdtemp(prefix="stage1b_test_")
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = TESTVOL
os.environ.pop("PRIVATE_UNIVERSES", None)
os.environ.pop("NIGHTLY_UNIVERSES", None)

import scanner_engine as se
import scan_store
import scheduler_engine as sched
import nightly_scan
import deep_dive_engine
import top100_engine
import top100_store


# ======================================================================
# T1: fetchers against saved Wikipedia-shaped HTML fixtures (synthetic).
# ======================================================================
def _wiki_table_html(tickers, sector_col_name="Sector"):
    df = pd.DataFrame({
        "Ticker": tickers,
        "Company": [f"Company {t}" for t in tickers],
        sector_col_name: ["Industrials"] * len(tickers),
    })
    return f"<html><body>{df.to_html(index=False)}</body></html>"


_ftse100_fixture_html = _wiki_table_html([f"FA{i:02d}" for i in range(100)], "FTSE Sector")
_ftse250_fixture_html = _wiki_table_html([f"FB{i:03d}" for i in range(230)], "FTSE Sector")
_tsx60_fixture_html = _wiki_table_html([f"TA{i:02d}" for i in range(55)])
_tsx_composite_fixture_html = _wiki_table_html([f"TA{i:02d}" for i in range(55)] + [f"TC{i:03d}" for i in range(140)])

with mock.patch.object(se, "_get", side_effect=lambda url: _ftse100_fixture_html):
    se.fetch_ftse100.clear()
    df_ftse100 = se.fetch_ftse100()
assert df_ftse100 is not None and len(df_ftse100) == 100, df_ftse100
assert set(df_ftse100["Ticker"]) == {f"FA{i:02d}.L" for i in range(100)}, sorted(df_ftse100["Ticker"])[:5]
print(f"[T1_fetch_ftse100] 100-row Wikipedia-shaped fixture -> {len(df_ftse100)} tickers, "
      "all normalised to the .L suffix OK")

with mock.patch.object(se, "_get", side_effect=lambda url: _ftse250_fixture_html):
    se.fetch_ftse250.clear()
    df_ftse250 = se.fetch_ftse250()
assert df_ftse250 is not None and len(df_ftse250) == 230, df_ftse250
print(f"[T1_fetch_ftse250] 230-row Wikipedia-shaped fixture -> {len(df_ftse250)} tickers OK")

with mock.patch.object(se, "_get", side_effect=lambda url: _tsx60_fixture_html):
    se.fetch_tsx60.clear()
    df_tsx60 = se.fetch_tsx60()
assert df_tsx60 is not None and len(df_tsx60) == 55, df_tsx60
assert set(df_tsx60["Ticker"]) == {f"TA{i:02d}.TO" for i in range(55)}
print(f"[T1_fetch_tsx60] 55-row Wikipedia-shaped fixture -> {len(df_tsx60)} tickers, "
      "all normalised to the .TO suffix OK")

with mock.patch.object(se, "_get", side_effect=lambda url: _tsx_composite_fixture_html), \
     mock.patch.object(se, "fetch_tsx60", return_value=df_tsx60):
    se.fetch_tsxcomposite.clear()
    df_tsxcomposite = se.fetch_tsxcomposite()
assert df_tsxcomposite is not None and len(df_tsxcomposite) == 195, df_tsxcomposite
assert set(df_tsx60["Ticker"]).issubset(set(df_tsxcomposite["Ticker"]))
print(f"[T1_fetch_tsxcomposite] 195-row Wikipedia-shaped fixture (TSX 60 unioned in) -> "
      f"{len(df_tsxcomposite)} tickers, TSX 60 is a subset OK")


# ======================================================================
# T2: verify_universe_before_save() - FTSE 250 no-overlap, TSX 60 subset.
# ======================================================================
assert "FTSE 250" in se.UNIVERSE_INTEGRITY_TRACKED_UNIVERSES
assert "TSX 60" in se.UNIVERSE_INTEGRITY_TRACKED_UNIVERSES
print("[T2_tracked] FTSE 250 (sibling check vs FTSE 100) and TSX 60 (subset check vs "
      "TSX Composite) are both in UNIVERSE_INTEGRITY_TRACKED_UNIVERSES - FTSE 100/TSX "
      "Composite are reference pools only, with no parent of their own to check OK")

_FTSE100_DF = pd.DataFrame({"Ticker": ["AAA.L", "BBB.L"], "Sector": ["Financials", "Energy"]})
_FTSE250_CLEAN = {"CCC.L", "DDD.L"}
_FTSE250_OVERLAP = {"CCC.L", "AAA.L"}

with mock.patch.object(se, "fetch_ftse100", return_value=_FTSE100_DF):
    ok, reason = se.verify_universe_before_save("FTSE 250", _FTSE250_CLEAN, log=None)
    assert ok is True, reason
    ok2, reason2 = se.verify_universe_before_save("FTSE 250", _FTSE250_OVERLAP, log=None)
    assert ok2 is False and "AAA.L" in reason2, (ok2, reason2)
print("[T2_ftse250_overlap] FTSE 250 disjoint-from-FTSE-100 accepted when clean, "
      "rejected when it overlaps FTSE 100 OK")

_TSX_COMPOSITE_DF = pd.DataFrame({
    "Ticker": ["RY.TO", "TD.TO", "ENB.TO", "SHOP.TO"], "Sector": ["Financials"] * 4,
})
with mock.patch.object(se, "fetch_tsxcomposite", return_value=_TSX_COMPOSITE_DF):
    ok3, reason3 = se.verify_universe_before_save("TSX 60", {"RY.TO", "TD.TO"}, log=None)
    assert ok3 is True, reason3
    ok4, reason4 = se.verify_universe_before_save("TSX 60", {"RY.TO", "XYZ.TO"}, log=None)
    assert ok4 is False and "XYZ.TO" in reason4, (ok4, reason4)
    ok5, reason5 = se.verify_universe_before_save("TSX 60", set(_TSX_COMPOSITE_DF["Ticker"]), log=None)
    assert ok5 is False and "identical" in reason5, reason5
print("[T2_tsx60_subset] TSX 60 strict-subset-of-TSX-Composite accepted when a genuine "
      "subset, rejected on an outside ticker, rejected when identical to the full "
      "Composite OK")


# ======================================================================
# T3: scan_store privacy primitives - default DENY vs PRIVATE_UNIVERSES.
# ======================================================================
os.environ.pop("PRIVATE_UNIVERSES", None)
assert scan_store.is_private_universe("FTSE 100") is True
assert scan_store.is_private_universe("TSX Composite") is True
assert scan_store.is_private_universe("S&P 500") is False
os.environ["PRIVATE_UNIVERSES"] = ""
assert scan_store.is_private_universe("FTSE 100") is False
os.environ["PRIVATE_UNIVERSES"] = "none"
assert scan_store.is_private_universe("FTSE 100") is False
os.environ.pop("PRIVATE_UNIVERSES", None)
print("[T3_is_private] FTSE 100/TSX Composite private by default, S&P 500 never - "
      "''/'none' clears privacy entirely OK")

_PUB_ROW = {
    "Ticker": "SHARED.AX", "Company Name": "Shared Co (public row)", "Price": 10.0,
    "Intrinsic Value": 11.0, "MOS %": 9.0, "Long Score": 55.0, "Quality": 50.0,
    "Psychology": 50.0, "Discovery (lite)": 50.0, "Sector": "Industrials",
}
_PRIV_ROW = {
    "Ticker": "SHARED.AX", "Company Name": "Shared Co (private row)", "Price": 10.0,
    "Intrinsic Value": 11.0, "MOS %": 9.0, "Long Score": 55.0, "Quality": 50.0,
    "Psychology": 50.0, "Discovery (lite)": 50.0, "Sector": "Industrials",
}
_PRIV_ONLY_ROW = {
    "Ticker": "PRIVONLY.L", "Company Name": "Private-only Co", "Price": 100.0,
    "Intrinsic Value": 120.0, "MOS %": 17.0, "Long Score": 95.0, "Quality": 90.0,
    "Psychology": 90.0, "Discovery (lite)": 90.0, "Sector": "Technology",
}
scan_store.save_scan("S&P 500", [_PUB_ROW], "fixture")
scan_store.save_scan("FTSE 100", [_PRIV_ROW, _PRIV_ONLY_ROW], "fixture")

assert "FTSE 100" not in scan_store.list_saved_universes()
assert "FTSE 100" in scan_store.list_saved_universes(include_private=True)
assert scan_store.load_scan("FTSE 100") is None
assert scan_store.load_scan_raw("FTSE 100") is None
assert scan_store.load_scan_raw("FTSE 100", allow_private=True) is not None
print("[T3_scan_store_deny] list_saved_universes()/load_scan()/load_scan_raw() all "
      "deny FTSE 100 by default, all serve it with allow_private=True OK")


# ======================================================================
# T5: a ticker saved under both a private and a public universe keeps
# its PUBLIC row by default.
# ======================================================================
found = scan_store.find_ticker_row("SHARED.AX")
assert found is not None and found["universe"] == "S&P 500", found
assert found["row"]["Company Name"] == "Shared Co (public row)", found["row"]
found_allow = scan_store.find_ticker_row("SHARED.AX", allow_private=True)
assert found_allow is not None
print("[T5_dual_universe] SHARED.AX (saved under both FTSE 100 and S&P 500) resolves "
      "to its PUBLIC (S&P 500) row by default OK")


# ======================================================================
# T4: byte-identical selection - select_top100_pool() with the private
# files present (default PRIVATE_UNIVERSES) vs the same files absent.
# ======================================================================
def _run_pool_and_cleanup():
    rows = top100_engine.select_top100_pool(log=lambda *a, **k: None)
    tickers = sorted(r["ticker"] for r in rows)
    return tickers


os.environ.pop("PRIVATE_UNIVERSES", None)
tickers_with_private_files = _run_pool_and_cleanup()
assert "PRIVONLY.L" not in tickers_with_private_files, tickers_with_private_files

_ftse100_path = scan_store._path("FTSE 100")
_saved_ftse100_bytes = open(_ftse100_path, "rb").read()
os.remove(_ftse100_path)
tickers_without_private_files = _run_pool_and_cleanup()

assert tickers_with_private_files == tickers_without_private_files, (
    tickers_with_private_files, tickers_without_private_files)
print(f"[T4_byte_identical] select_top100_pool() output is IDENTICAL "
      f"({len(tickers_with_private_files)} ticker(s)) whether FTSE 100's saved scan "
      f"file exists (default PRIVATE_UNIVERSES, excluded) or doesn't exist at all OK")

with open(_ftse100_path, "wb") as f:
    f.write(_saved_ftse100_bytes)

os.environ["PRIVATE_UNIVERSES"] = ""
tickers_revealed = _run_pool_and_cleanup()
assert "PRIVONLY.L" in tickers_revealed, tickers_revealed
assert "SHARED.AX" in tickers_revealed, tickers_revealed
os.environ.pop("PRIVATE_UNIVERSES", None)
print("[T4_revealed] with PRIVATE_UNIVERSES='' PRIVONLY.L/SHARED.AX DO appear in "
      "select_top100_pool()'s output OK")


# ======================================================================
# T6: price_unit_suspect -> no Intrinsic Value/MOS, with a reason, in
# BOTH the Deep Dive headline and the nightly-scan row.
# ======================================================================
_SUS_SHARES = 1_000_000
_SUS_PRICE_PENCE = 2600.0
_sus_info = {
    "currency": "GBp", "financialCurrency": "GBP",
    "currentPrice": _SUS_PRICE_PENCE, "regularMarketPrice": _SUS_PRICE_PENCE,
    # Corrupted marketCap (still pence-scale after the price itself is
    # correctly divided to pounds) - the exact live SHEL.L/TSCO.L
    # SUSPECT shape the guard exists to catch (ratio 0.01, not ~1.00).
    "marketCap": _SUS_PRICE_PENCE * _SUS_SHARES,
    "sharesOutstanding": _SUS_SHARES, "longName": "Suspect-shaped",
}
_sus_cf = pd.DataFrame(
    {f"202{6 - i}-06-30": [14_000_000.0, -1_400_000.0] for i in range(5)},
    index=["Operating Cash Flow", "Capital Expenditure"],
)
_sus_dates = pd.date_range("2026-04-01", periods=130, freq="D")
_sus_hist = pd.DataFrame({
    "Open": [_SUS_PRICE_PENCE] * 130, "High": [_SUS_PRICE_PENCE * 1.01] * 130,
    "Low": [_SUS_PRICE_PENCE * 0.99] * 130, "Close": [_SUS_PRICE_PENCE] * 130,
    "Volume": [1_000_000] * 130,
}, index=_sus_dates)

_sus_dd_log = io.StringIO()
with contextlib.redirect_stdout(_sus_dd_log):
    _sus_dd = deep_dive_engine.analyze(
        "SUSPECT.L",
        get_price_history=lambda t: _sus_hist,
        get_ticker_info=lambda t: dict(_sus_info),
        get_cashflow_df=lambda t: _sus_cf,
        live_data=False, enable_social=False,
    )
assert _sus_dd["price_unit_suspect"] is True, _sus_dd
assert _sus_dd["price_unit_suspect_reason"], _sus_dd
assert _sus_dd["intrinsic_value"] is None, _sus_dd["intrinsic_value"]
assert _sus_dd["mos"] is None, _sus_dd["mos"]
print("[T6_deep_dive_suspect] a price_unit_suspect fixture's Deep Dive headline withholds "
      f"Intrinsic Value/MOS (reason={_sus_dd['price_unit_suspect_reason']!r}) OK")


class _SusFakeTicker:
    def __init__(self, *_a, **_k):
        self.info = dict(_sus_info)
        self.cashflow = _sus_cf
        self.dividends = pd.Series(dtype=float)

    def history(self, *a, **k):
        return _sus_hist


with mock.patch.object(nightly_scan, "yf") as _myf:
    _myf.Ticker.side_effect = _SusFakeTicker
    _sus_row = nightly_scan.analyze_ticker_lite("SUSPECT.L")
assert _sus_row is not None
assert _sus_row["price_unit_suspect"] is True, _sus_row
assert _sus_row["price_unit_suspect_reason"], _sus_row
assert _sus_row["Intrinsic Value"] is None, _sus_row["Intrinsic Value"]
assert _sus_row["MOS %"] is None, _sus_row["MOS %"]
print("[T6_scan_row_suspect] the same fixture's nightly-scan row also withholds "
      f"Intrinsic Value/MOS % (reason={_sus_row['price_unit_suspect_reason']!r}) OK")


# ======================================================================
# T7: _DEFAULT_NIGHTLY_UNIVERSES before/after schedule table.
# ======================================================================
_BEFORE_NIGHTLY_UNIVERSES = (
    "ASX 200:daily, ASX 300:daily, ASX All Technology:daily, S&P 500:daily, "
    "Nasdaq 100:daily, Dow Jones 30:daily, All Ordinaries:mon, "
    "S&P 400 MidCap:tue, Small Caps (S&P 600):wed, Russell 1000:thu, "
    "S&P 500 Dividend Aristocrats:fri, Russell 2000:sat"
)
_before_map = sched._parse_nightly_universes(_BEFORE_NIGHTLY_UNIVERSES)
_after_map = sched._parse_nightly_universes(sched._DEFAULT_NIGHTLY_UNIVERSES)

print("[T7_schedule] universe             before    after")
for u, cadence in _before_map.items():
    after_cadence = _after_map.get(u)
    print(f"    {u:28s} {cadence:8s} {after_cadence}")
    assert after_cadence == cadence, (u, cadence, after_cadence)
_new_entries = {u: c for u, c in _after_map.items() if u not in _before_map}
for u, cadence in _new_entries.items():
    print(f"    {u:28s} {'(new)':8s} {cadence}")
# Containment, not exact equality: Stage 1 Japan's own later Commit B
# legitimately adds two more entries (Nikkei 225:mon, TOPIX 500:fri) on
# top of this stage's own 4 - this check's own job is only to confirm
# THIS stage's 4 entries are present with the right cadence and that
# nothing pre-existing changed, not that nothing else was ever added
# after this stage shipped.
_stage1b_entries = {
    "FTSE 100": "sun", "FTSE 250": "sun", "TSX 60": "tue", "TSX Composite": "tue",
}
for u, cadence in _stage1b_entries.items():
    assert _new_entries.get(u) == cadence, (u, cadence, _new_entries.get(u))
print("[T7_schedule] every pre-existing entry's cadence is unchanged; the 4 new "
      "entries land on sun (FTSE)/tue (TSX) exactly as specified OK")


# ======================================================================
# T8: scheduler_engine._run_nightly() skips the public snapshot/alert
# side effects for a private universe, but still runs insider_engine.
# refresh_universe() (deliberately left ungated).
# ======================================================================
_fake_payload = {"rows": [_PRIV_ONLY_ROW], "attention_lite": False}
_calls = {"snapshot": 0, "alert": 0, "insider": 0}

with mock.patch.object(nightly_scan, "run_universe_scan", return_value=_fake_payload), \
     mock.patch.object(nightly_scan, "refresh_market_cap_ranking", return_value=None), \
     mock.patch.object(sched, "_load_state", return_value={}), \
     mock.patch.object(sched, "_save_state", return_value=None):
    import alert_engine
    import snapshot_store
    import insider_engine
    with mock.patch.object(alert_engine, "snapshot_previous_values", return_value={}), \
         mock.patch.object(alert_engine, "check_universe_rows",
                            side_effect=lambda *a, **k: _calls.__setitem__("alert", _calls["alert"] + 1)), \
         mock.patch.object(snapshot_store, "build_snapshots_from_scan",
                            side_effect=lambda *a, **k: _calls.__setitem__("snapshot", _calls["snapshot"] + 1)), \
         mock.patch.object(insider_engine, "refresh_universe",
                            side_effect=lambda *a, **k: _calls.__setitem__("insider", _calls["insider"] + 1)):
        sched._run_nightly({"universes": ["FTSE 100"]}, log=lambda *a, **k: None, run_night="2026-10-03")

assert _calls["snapshot"] == 0, _calls
assert _calls["alert"] == 0, _calls
assert _calls["insider"] == 1, _calls
print("[T8_run_nightly_gate] _run_nightly() on a private universe never calls "
      "snapshot_store.build_snapshots_from_scan()/alert_engine.check_universe_rows(), "
      "but still runs insider_engine.refresh_universe() (left ungated) OK")


# ======================================================================
# T9: "real" before/after vs commit de575e6 (Stage 1a-fix, immediately
# before this Stage 1b task) - the 7 Director-Q&A fixtures (5 existing
# + the 2 new bank fixtures) run against the REAL old code and this
# commit's current code.
# ======================================================================
def _load_module_from_git(path, module_name, commit):
    src = subprocess.run(
        ["git", "show", f"{commit}:{path}"], cwd=REPO_ROOT,
        capture_output=True, text=True, check=True,
    ).stdout
    spec = importlib.util.spec_from_loader(module_name, loader=None)
    mod = importlib.util.module_from_spec(spec)
    mod.__file__ = f"<git {commit}:{path}>"
    sys.modules[module_name] = mod
    exec(compile(src, mod.__file__, "exec"), mod.__dict__)
    return mod


_PRE_STAGE1B_COMMIT = "de575e6"
_t9_usd_fixtures = [("ADP", 120_000_000_000), ("CPRT", 45_000_000_000), ("AOS", 9_000_000_000)]
_t9_aud_fixtures = [("CSL.AX", 130_000_000_000), ("OCL.AX", 900_000_000)]
_t9_bank_fixtures = [("US_BANK", 60_000_000_000), ("AX_BANK.AX", 20_000_000_000)]

_saved_capm = sys.modules.pop("capm_engine", None)
_saved_fcf = sys.modules.pop("fcf_valuation_engine", None)
try:
    old_capm = _load_module_from_git("capm_engine.py", "capm_engine", _PRE_STAGE1B_COMMIT)
    old_fcf = _load_module_from_git("fcf_valuation_engine.py", "fcf_valuation_engine", _PRE_STAGE1B_COMMIT)
    assert old_capm._DISCOUNT_TIER_FX_TO_USD_APPROX["GBP"] == 1.33, (
        "sanity check: the commit loaded must genuinely predate Stage 1b's FX constant change")

    _old_results = {}
    with mock.patch.object(old_capm, "get_risk_free_rate", return_value=(0.050, "default")):
        for ticker, mcap in _t9_usd_fixtures + _t9_bank_fixtures:
            if ticker.endswith(".AX"):
                continue
            info = {"currency": "USD", "marketCap": mcap}
            rate, _meta = old_capm.resolve_discount_rate_by_market_cap(info, "USD")
            _old_results[(ticker, "discount")] = rate
    with mock.patch.object(old_capm, "get_au_risk_free_rate_live", return_value=(0.053, "default")):
        for ticker, mcap in _t9_aud_fixtures + _t9_bank_fixtures:
            if not ticker.endswith(".AX"):
                continue
            info = {"currency": "AUD", "marketCap": mcap}
            rate, _meta = old_capm.resolve_discount_rate_by_market_cap(info, "AUD")
            _old_results[(ticker, "discount")] = rate
    for ticker, mcap in _t9_usd_fixtures + _t9_aud_fixtures + _t9_bank_fixtures:
        ccy = "AUD" if ticker.endswith(".AX") else "USD"
        info = {"currency": ccy, "marketCap": mcap}
        _old_results[(ticker, "ceiling")] = old_fcf.growth_ceiling_for(info)
        _old_results[(ticker, "end_rate")] = old_fcf.growth_end_rate_for(info)
finally:
    if _saved_capm is not None:
        sys.modules["capm_engine"] = _saved_capm
    else:
        sys.modules.pop("capm_engine", None)
    if _saved_fcf is not None:
        sys.modules["fcf_valuation_engine"] = _saved_fcf
    else:
        sys.modules.pop("fcf_valuation_engine", None)

import capm_engine as new_capm
import fcf_valuation_engine as new_fcf

assert new_capm._DISCOUNT_TIER_FX_TO_USD_APPROX["GBP"] == 1.32
assert new_capm._DISCOUNT_TIER_FX_TO_USD_APPROX["CAD"] == 0.70

_new_results = {}
with mock.patch.object(new_capm, "get_risk_free_rate", return_value=(0.050, "default")):
    for ticker, mcap in _t9_usd_fixtures + _t9_bank_fixtures:
        if ticker.endswith(".AX"):
            continue
        info = {"currency": "USD", "marketCap": mcap}
        rate, _meta = new_capm.resolve_discount_rate_by_market_cap(info, "USD")
        _new_results[(ticker, "discount")] = rate
with mock.patch.object(new_capm, "get_au_risk_free_rate_live", return_value=(0.053, "default")):
    for ticker, mcap in _t9_aud_fixtures + _t9_bank_fixtures:
        if not ticker.endswith(".AX"):
            continue
        info = {"currency": "AUD", "marketCap": mcap}
        rate, _meta = new_capm.resolve_discount_rate_by_market_cap(info, "AUD")
        _new_results[(ticker, "discount")] = rate
for ticker, mcap in _t9_usd_fixtures + _t9_aud_fixtures + _t9_bank_fixtures:
    ccy = "AUD" if ticker.endswith(".AX") else "USD"
    info = {"currency": ccy, "marketCap": mcap}
    _new_results[(ticker, "ceiling")] = new_fcf.growth_ceiling_for(info)
    _new_results[(ticker, "end_rate")] = new_fcf.growth_end_rate_for(info)

print(f"[T9_real_before_after] every USD/AUD/bank fixture run against the REAL code at "
      f"commit {_PRE_STAGE1B_COMMIT} (before Stage 1b) and against this commit's current code:")
_t9_all_match = True
for ticker, mcap in _t9_usd_fixtures + _t9_aud_fixtures + _t9_bank_fixtures:
    for metric in ("discount", "ceiling", "end_rate"):
        before = _old_results[(ticker, metric)]
        after = _new_results[(ticker, metric)]
        match = before == after
        _t9_all_match = _t9_all_match and match
        print(f"    {ticker:12s} {metric:9s} before={before:.4f}  after={after:.4f}  "
              f"{'MATCH' if match else 'MISMATCH'}")
        assert match, (ticker, metric, before, after)
assert _t9_all_match
print(f"[T9_real_before_after] every metric MATCH against commit {_PRE_STAGE1B_COMMIT} - "
      "Stage 1b changed nothing USD/AUD-reaching OK")

print("\nAll Stage 1b private-universes checks passed.")
