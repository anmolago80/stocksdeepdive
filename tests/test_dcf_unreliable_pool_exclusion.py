"""
DCF-unreliable pool exclusion (owner-directed, 1 Oct 2026, Commit 1 of
instruction_dcf_unreliable_pool_step4_nightly.md, acting on the growth/
PE-forward task's own Part B diagnostic finding).

Fact: nightly_scan.py writes "DCF Unreliable" (resolver_engine.
dcf_looks_unreliable() - intrinsic value > DCF_SANITY_MULTIPLE=3.0x the
price, i.e. MOS > 66.7%) into every scan row, but top100_engine never
read it - the Value Score's MOS component clamps at 50, so a 97% MOS
row collects the full 50 points and gets pushed into the pool purely on
a model artefact, costing an Opus call that usually comes back NOT
RATED. 8 of the 12 names on the reported shelf were already flagged
this way (GLS.AX, HAPN, LSF.AX, SLDE, TOYO, REG.AX, CRMD, ASIC).

Fix: top100_engine.select_top100_pool() skips any candidate row whose
"DCF Unreliable" is truthy BEFORE building best_by_ticker - pool
eligibility only, no change to _recompute_value_score()/
calculate_long_score()/MOS clamp/stored scores. Logs one summary line:
"[top100] selection: excluded N DCF-unreliable row(s): T1, T2, ...".

This file covers:
  - excluded rows never enter the pool; non-excluded rows' Value Scores
    are byte-identical to a baseline run where the excluded rows never
    existed at all (proves the exclusion changes NOTHING about how any
    other row scores)
  - the summary log line names every excluded ticker
  - back-fill: with a small POOL_SIZE, an excluded row's slot is filled
    by the next-best valid candidate, not left empty
  - a ticker with two candidate rows (one flagged, one clean) across
    two universes still enters the pool via its clean row
  - the ASX extension (drawn from the same best_by_ticker dict) honours
    the same exclusion
  - the defensive end-of-function assertion fires if a DCF-unreliable
    row somehow reached the final pool/extension list
  - top100_render._dcf_unreliable_chip_html() renders the chip for a
    row shaped like a pre-fix-saved DCF-unreliable pool row, and stays
    empty for a normal row

Run: python3 tests/test_dcf_unreliable_pool_exclusion.py
"""
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TESTVOL = tempfile.mkdtemp(prefix="dcf_unreliable_pool_test_")
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = TESTVOL

import scan_store
import top100_engine as te
import top100_render as tr
import top100_store as ts

if os.path.exists(ts.DB_PATH):
    os.remove(ts.DB_PATH)

_now_dt = datetime.now(timezone.utc)
_FAKE_CADENCE = {"S&P 500": "daily", "ASX 200": "daily", "Nasdaq 100": "daily"}

# Every fixture row below omits "Sector", which would otherwise send
# select_top100_pool() into _fill_missing_sectors()'s real yfinance
# fallback for every ticker (one network call + PER_TICKER_SLEEP each) -
# irrelevant to this task (sector display, not pool exclusion) and slow/
# blocked in a sandboxed test run, so it's mocked out everywhere in this
# file via a module-level patch applied to every select_top100_pool()
# call below.
_patch_sector_fill = mock.patch.object(te, "_fill_missing_sectors", lambda rows, log=print: None)
_patch_sector_fill.start()


def _row(ticker, long_score, quality=70, mos=20.0, psychology=5.0, discovery=10.0,
         price=50.0, iv=55.0, dcf_unreliable=False):
    return {
        "Ticker": ticker,
        "Company Name": f"{ticker} Co",
        "Quality": quality,
        "MOS %": mos,
        "Psychology": psychology,
        "Discovery (lite)": discovery,
        "Long Score": long_score,
        "Price": price,
        "Intrinsic Value": iv,
        "DCF Unreliable": dcf_unreliable,
    }


def _save_universe(universe, rows, generated_at, degraded=False):
    payload = {
        "universe": universe, "source": "test", "generated_at": generated_at.isoformat(),
        "run_night": None, "rows": rows, "attention_lite": True, "degraded": degraded,
    }
    tmp = scan_store._path(universe) + ".tmp"
    with open(tmp, "w") as f:
        json.dump(payload, f)
    os.replace(tmp, scan_store._path(universe))


def _clear(*universes):
    for u in universes:
        p = scan_store._path(u)
        if os.path.exists(p):
            os.remove(p)


# ======================================================================
# CHECK 1: 3 DCF-unreliable rows among 8 clean candidates - excluded
# rows never enter the pool; every clean row's value_score is
# byte-identical to a baseline run where the 3 excluded rows never
# existed. Log line names all 3 excluded tickers.
# ======================================================================
_clean_rows = [_row(f"CLEAN{i}", long_score=50.0 + i, mos=20.0) for i in range(8)]
_unreliable_rows = [
    _row("BAD1", long_score=95.0, mos=70.0, price=10.0, iv=97.0, dcf_unreliable=True),
    _row("BAD2", long_score=93.0, mos=70.0, price=20.0, iv=90.0, dcf_unreliable=True),
    _row("BAD3", long_score=91.0, mos=70.0, price=5.0, iv=40.0, dcf_unreliable=True),
]
_save_universe("S&P 500", _clean_rows + _unreliable_rows, _now_dt)

_logs1 = []
with mock.patch.object(te.scheduler_engine, "nightly_universe_cadence",
                        return_value=dict(_FAKE_CADENCE)):
    pool1 = te.select_top100_pool(log=_logs1.append)

pool1_tickers = {r["ticker"] for r in pool1}
assert not ({"BAD1", "BAD2", "BAD3"} & pool1_tickers), pool1_tickers
assert {"CLEAN0", "CLEAN1", "CLEAN2", "CLEAN3", "CLEAN4", "CLEAN5", "CLEAN6", "CLEAN7"} <= pool1_tickers

_log_line = next(l for l in _logs1 if "DCF-unreliable" in l)
assert "excluded 3 DCF-unreliable row(s)" in _log_line, _log_line
for t in ("BAD1", "BAD2", "BAD3"):
    assert t in _log_line, _log_line
print(f"[excluded_rows_never_enter_pool] 3 DCF-unreliable rows (BAD1/BAD2/BAD3) excluded, "
      f"8 clean rows all present; log line: {_log_line!r} OK")

_clear("S&P 500")
_save_universe("S&P 500", _clean_rows, _now_dt)
with mock.patch.object(te.scheduler_engine, "nightly_universe_cadence",
                        return_value=dict(_FAKE_CADENCE)):
    pool1_baseline = te.select_top100_pool(log=lambda *a, **k: None)
_scores_with_excluded = {r["ticker"]: r["value_score"] for r in pool1 if r["ticker"] in pool1_tickers}
_scores_baseline = {r["ticker"]: r["value_score"] for r in pool1_baseline}
for t in _scores_baseline:
    assert _scores_with_excluded[t] == _scores_baseline[t], (t, _scores_with_excluded[t], _scores_baseline[t])
print("[clean_row_scores_byte_identical] every CLEAN ticker's value_score is byte-identical "
      "whether or not the 3 excluded rows were present in the same scan file OK")

_clear("S&P 500")


# ======================================================================
# CHECK 2: back-fill - with POOL_SIZE patched down to 3, excluding 2 of
# the top 5 candidates by score still yields a FULL 3-row pool (the
# next-best valid candidates fill the freed slots), not a short one.
# ======================================================================
_backfill_rows = [
    _row("TOP1", long_score=99.0, mos=70.0, price=10.0, iv=97.0, dcf_unreliable=True),
    _row("TOP2", long_score=98.0, mos=20.0),
    _row("TOP3", long_score=97.0, mos=70.0, price=10.0, iv=97.0, dcf_unreliable=True),
    _row("TOP4", long_score=96.0, mos=20.0),
    _row("TOP5", long_score=95.0, mos=20.0),
]
_save_universe("S&P 500", _backfill_rows, _now_dt)
with mock.patch.object(te.scheduler_engine, "nightly_universe_cadence",
                        return_value=dict(_FAKE_CADENCE)), \
     mock.patch.object(te, "POOL_SIZE", 3):
    pool2 = te.select_top100_pool(log=lambda *a, **k: None)
pool2_tickers = {r["ticker"] for r in pool2}
assert len(pool2) == 3, pool2
assert pool2_tickers == {"TOP2", "TOP4", "TOP5"}, pool2_tickers
print(f"[backfill_fills_freed_slots] POOL_SIZE=3, TOP1/TOP3 excluded (ranks #1/#3 by raw "
      f"score) -> pool still has exactly 3 rows ({sorted(pool2_tickers)}), backfilled from "
      f"the next-best valid candidates, not left short OK")

_clear("S&P 500")


# ======================================================================
# CHECK 3: a ticker with two candidate rows (one flagged, one clean)
# across two universes still enters the pool via its clean row - the
# exclusion is per-ROW, not a blanket per-TICKER ban.
# ======================================================================
_save_universe("S&P 500", [_row("DUAL", long_score=60.0, dcf_unreliable=True, mos=70.0,
                                 price=10.0, iv=97.0)], _now_dt - timedelta(hours=1))
_save_universe("Nasdaq 100", [_row("DUAL", long_score=55.0, dcf_unreliable=False)], _now_dt)
with mock.patch.object(te.scheduler_engine, "nightly_universe_cadence",
                        return_value=dict(_FAKE_CADENCE)):
    pool3 = te.select_top100_pool(log=lambda *a, **k: None)
dual = next((r for r in pool3 if r["ticker"] == "DUAL"), None)
assert dual is not None, "DUAL should still enter the pool via its clean Nasdaq 100 row"
assert dual["universe"] == "Nasdaq 100", dual["universe"]
print("[per_row_not_per_ticker] DUAL has one flagged row (S&P 500) and one clean row "
      "(Nasdaq 100, also fresher) - still enters the pool via the clean row OK")

_clear("S&P 500", "Nasdaq 100")


# ======================================================================
# CHECK 4: ASX extension (drawn from the same best_by_ticker dict)
# honours the same exclusion - a flagged .AX row never fills an
# extension slot.
# ======================================================================
_au_rows = [_row(f"AU{i}.AX", long_score=40.0 + i, mos=15.0) for i in range(5)]
_au_rows.append(_row("AUBAD.AX", long_score=99.0, mos=70.0, price=10.0, iv=97.0,
                      dcf_unreliable=True))
_save_universe("ASX 200", _au_rows, _now_dt)
with mock.patch.object(te.scheduler_engine, "nightly_universe_cadence",
                        return_value=dict(_FAKE_CADENCE)), \
     mock.patch.object(te, "POOL_SIZE", 1):
    pool4 = te.select_top100_pool(log=lambda *a, **k: None)
extension4 = ts.current_asx_extension()
extension_tickers = {r["ticker"] for r in extension4}
assert "AUBAD.AX" not in extension_tickers, extension_tickers
assert "AUBAD.AX" not in {r["ticker"] for r in pool4}
print(f"[asx_extension_honours_exclusion] AUBAD.AX (DCF-unreliable, highest raw score) "
      f"never fills an ASX extension slot ({sorted(extension_tickers)}) OK")

_clear("ASX 200")


# ======================================================================
# CHECK 5: defensive assertion - if a DCF-unreliable row somehow
# survived to the final pool/extension list (simulated by monkey-
# patching _is_dcf_unreliable to always return False, i.e. "the
# per-row skip is broken"), the loud assertion before save_pool()
# fires rather than silently persisting the artefact.
# ======================================================================
_save_universe("S&P 500", [_row("SNEAKY", long_score=80.0, mos=70.0, price=10.0, iv=97.0,
                                 dcf_unreliable=True)], _now_dt)
_raised = False
try:
    with mock.patch.object(te.scheduler_engine, "nightly_universe_cadence",
                            return_value=dict(_FAKE_CADENCE)), \
         mock.patch.object(te, "_is_dcf_unreliable", return_value=False):
        te.select_top100_pool(log=lambda *a, **k: None)
except AssertionError as e:
    _raised = True
    assert "SNEAKY" in str(e), e
assert _raised, "the defensive assertion should have fired when the per-row skip is bypassed"
print("[defensive_assertion_fires] with the per-row skip simulated as broken, the "
      "end-of-function assertion catches the DCF-unreliable row before save_pool() - "
      "loud failure, not a silent re-introduction of the artefact OK")

_clear("S&P 500")


# ======================================================================
# CHECK 6: render-layer defensive chip - top100_render._dcf_unreliable_
# chip_html() renders the amber chip for a row shaped like a pre-fix-
# saved pool row (intrinsic_value/price only, no "DCF Unreliable" raw
# field at all - the curated row shape never carries that field),
# empty string for a normal row.
# ======================================================================
_bad_curated_row = {"ticker": "OLDBAD", "intrinsic_value": 97.0, "price": 10.0}
_good_curated_row = {"ticker": "OLDGOOD", "intrinsic_value": 60.0, "price": 50.0}
chip_bad = tr._dcf_unreliable_chip_html(_bad_curated_row, "en")
chip_good = tr._dcf_unreliable_chip_html(_good_curated_row, "en")
assert "DCF unreliable" in chip_bad, chip_bad
assert chip_good == "", chip_good
chip_bad_es = tr._dcf_unreliable_chip_html(_bad_curated_row, "es")
assert "DCF no confiable" in chip_bad_es, chip_bad_es
print("[render_defensive_chip] a pre-fix-shaped pool row (intrinsic_value=97, price=10, "
      "ratio 9.7x) renders the amber chip EN+ES; a normal row renders nothing OK")

_patch_sector_fill.stop()

print("\nALL DCF-UNRELIABLE POOL EXCLUSION FIXTURES PASSED")
