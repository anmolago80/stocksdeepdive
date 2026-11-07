"""
TASK 37 (Director-directed, 6 Nov 2026, ADBE case): a high-quality
exception to the Top 200's "model unreliable" (intrinsic value >
3x price) exclusion - behind TOP200_QUALITY_EXCEPTION_LIVE, unset/"0"
= today's behaviour exactly, off by default, never set here.

EVIDENCE (owner, 5 Nov S&P 500 scan): ADBE price 246.44, intrinsic
value 750.64 (3.05x), MOS 67.2%, Quality 87, Moat 95, DCF Unreliable
= True -> excluded today by top100_engine._is_dcf_unreliable()/
resolver_engine.dcf_looks_unreliable(). Its Top 200 score would be
62.8 - MOS is already clamped at 50 in the Value Score, so admitting
such a row can never inflate its score.

Covers:
  - top100_engine._quality_exception_admits() - ALL conditions (Moat
    >= 80, Quality >= 70, Share Count Flagged False, FCF Source real
    statement history, Trading Status not stale, Intrinsic Source ==
    "dcf") individually, including the ADBE-shaped admit case.
  - top100_engine.top200_quality_exception_live() - the env gate,
    re-read per call.
  - _build_best_by_ticker()/select_top100_pool() integration: switch
    OFF -> byte-identical to today (ADBE-shaped row still excluded,
    "quality_exception_admitted" False on every row); switch ON (via
    quality_exception_override=True, never via the env var) -> ADBE-
    shaped row admitted, a 3x row with moat 60 or quality 55 still
    excluded, a share-count-flagged or info-FCF row still excluded.
  - top100_store.save_pool()/current_pool()/is_quality_exception_
    admitted() round-trip for the new column.
  - top100_render._quality_exception_chip_html() - EN+ES text, shown
    only when the row's stored flag is set, alongside (not instead
    of) _dcf_unreliable_chip_html()'s own warning.

Run: python3 tests/test_director_task37_quality_exception.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TESTVOL = tempfile.mkdtemp(prefix="task37_test_")
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = TESTVOL
os.environ.pop("TOP200_QUALITY_EXCEPTION_LIVE", None)
os.environ.pop("TOP200_MOAT_BLEND_LIVE", None)
os.environ.pop("TOP200_BACKFILL_LIVE", None)
os.environ.pop("PRIVATE_UNIVERSES", None)

import resolver_engine
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


def _adbe_row(ticker="ADBE", **overrides):
    """ADBE's own reported fixture (5 Nov S&P 500 scan) - price 246.44,
    intrinsic value 750.64 (3.05x, DCF-unreliable), Quality 87, Moat
    95, every OTHER exception condition satisfied by default
    (Share Count Flagged False, FCF Source a real statement-derived
    value, Trading Status None (not stale), Intrinsic Source "dcf").
    `overrides` lets each check flip exactly one condition."""
    row = {
        "Ticker": ticker, "Company Name": f"{ticker} Co", "Price": 246.44,
        "Intrinsic Value": 750.64, "MOS %": 67.2, "Long Score": 62.8,
        "Quality": 87.0, "Moat": 95.0, "Psychology": 5.0, "Discovery (lite)": 20.0,
        "FCF Source": "ocf-normcapex", "Share Count Flagged": False,
        "Trading Status": None, "Intrinsic Source": "dcf",
        "Is Fund": False, "price_unit_suspect": False, "Sector": "Technology",
        "DCF Unreliable": True,
    }
    row.update(overrides)
    return row


# ======================================================================
# CHECK 1: _quality_exception_admits() - the ADBE-shaped row admits;
# each individual condition's failure alone excludes it.
# ======================================================================
check("ADBE-shaped row (moat 95, quality 87, clean FCF/share-count/"
      "trading/intrinsic-source) is admitted",
      te._quality_exception_admits(_adbe_row()))
check("Moat just below the 80 threshold (60) -> NOT admitted",
      not te._quality_exception_admits(_adbe_row(Moat=60.0)))
check("Quality just below the 70 threshold (55) -> NOT admitted",
      not te._quality_exception_admits(_adbe_row(Quality=55.0)))
check("Moat exactly at threshold (80) -> admitted (>=, not >)",
      te._quality_exception_admits(_adbe_row(Moat=80.0)))
check("Quality exactly at threshold (70) -> admitted (>=, not >)",
      te._quality_exception_admits(_adbe_row(Quality=70.0)))
check("Share Count Flagged True -> NOT admitted",
      not te._quality_exception_admits(_adbe_row(**{"Share Count Flagged": True})))
check("FCF Source 'info' -> NOT admitted (not real statement history)",
      not te._quality_exception_admits(_adbe_row(**{"FCF Source": "info"})))
check("FCF Source 'none' -> NOT admitted",
      not te._quality_exception_admits(_adbe_row(**{"FCF Source": "none"})))
check("FCF Source 'manual' -> NOT admitted",
      not te._quality_exception_admits(_adbe_row(**{"FCF Source": "manual"})))
check("FCF Source 'net_income_financials' (real statement history) -> admitted",
      te._quality_exception_admits(_adbe_row(**{"FCF Source": "net_income_financials"})))
check("FCF Source 'ocf_fallback_financials' (real statement history) -> admitted",
      te._quality_exception_admits(_adbe_row(**{"FCF Source": "ocf_fallback_financials"})))
check("Trading Status 'stale' -> NOT admitted",
      not te._quality_exception_admits(_adbe_row(**{"Trading Status": "stale"})))
check("Intrinsic Source 'pe-blend' -> NOT admitted (never a real DCF)",
      not te._quality_exception_admits(_adbe_row(**{"Intrinsic Source": "pe-blend"})))
check("Missing Moat entirely -> NOT admitted, never raises",
      not te._quality_exception_admits({k: v for k, v in _adbe_row().items() if k != "Moat"}))
check("Missing Quality entirely -> NOT admitted, never raises",
      not te._quality_exception_admits({k: v for k, v in _adbe_row().items() if k != "Quality"}))
print("[task37_check1_admits_predicate] _quality_exception_admits() - every "
      "condition individually gates admission, ADBE's own shape admits OK")


# ======================================================================
# CHECK 2: top200_quality_exception_live() - the env gate, re-read.
# ======================================================================
os.environ.pop("TOP200_QUALITY_EXCEPTION_LIVE", None)
check("unset -> False", te.top200_quality_exception_live() is False)
os.environ["TOP200_QUALITY_EXCEPTION_LIVE"] = "1"
check("'1' -> True", te.top200_quality_exception_live() is True)
os.environ["TOP200_QUALITY_EXCEPTION_LIVE"] = "0"
check("'0' -> False", te.top200_quality_exception_live() is False)
os.environ.pop("TOP200_QUALITY_EXCEPTION_LIVE", None)
print("[task37_check2_gate] top200_quality_exception_live() re-reads the env "
      "var correctly every call OK")


# ======================================================================
# CHECK 3: _build_best_by_ticker() - switch OFF (quality_exception_
# override=None, env unset - today's REAL live state): the ADBE-shaped
# row stays excluded exactly as before, "quality_exception_admitted"
# is False on every surviving row.
# ======================================================================
if os.path.exists(ts.DB_PATH):
    os.remove(ts.DB_PATH)
_clean_rows = [_adbe_row(f"CLEAN{i}", **{
    "Ticker": f"CLEAN{i}", "Price": 100.0, "Intrinsic Value": 110.0,
    "DCF Unreliable": False, "Long Score": 50.0 + i,
}) for i in range(5)]
scan_store.save_scan("S&P 500", _clean_rows + [_adbe_row()], source_label="test fixture")

os.environ.pop("TOP200_QUALITY_EXCEPTION_LIVE", None)
_best_off, _ = te._build_best_by_ticker(log=lambda *a, **k: None)
check("switch OFF: ADBE-shaped DCF-unreliable row is NOT in best_by_ticker "
      "(excluded exactly as today)", "ADBE" not in _best_off)
check("switch OFF: every surviving row's quality_exception_admitted is False",
      all(not r.get("quality_exception_admitted") for r in _best_off.values()))
print("[task37_check3_switch_off_byte_identical] switch OFF -> ADBE-shaped "
      "row excluded exactly as today, no row ever flagged admitted OK")


# ======================================================================
# CHECK 4: _build_best_by_ticker(quality_exception_override=True) -
# the ADBE-shaped row IS admitted, flagged, and carries a sane
# Value Score (never raises/blows up the pool on admission).
# ======================================================================
_best_on, _ = te._build_best_by_ticker(log=lambda *a, **k: None, quality_exception_override=True)
check("switch ON (forced): ADBE-shaped row IS in best_by_ticker",
      "ADBE" in _best_on)
check("switch ON (forced): ADBE row's quality_exception_admitted is True",
      _best_on.get("ADBE", {}).get("quality_exception_admitted") is True)
check("switch ON (forced): clean rows are COMPLETELY unaffected",
      all(f"CLEAN{i}" in _best_on and not _best_on[f"CLEAN{i}"]["quality_exception_admitted"]
          for i in range(5)))
print("[task37_check4_switch_on_admits_adbe] quality_exception_override=True "
      "admits the ADBE-shaped row and flags it, leaves clean rows untouched OK")


# ======================================================================
# CHECK 5: a 3x row with moat 60 or quality 55 (below either threshold)
# STAYS EXCLUDED even with the switch forced on; a share-count-flagged
# or info-FCF row stays excluded too.
# ======================================================================
if os.path.exists(ts.DB_PATH):
    os.remove(ts.DB_PATH)
_low_moat = _adbe_row("LOWMOAT", **{"Ticker": "LOWMOAT", "Moat": 60.0})
_low_quality = _adbe_row("LOWQ", **{"Ticker": "LOWQ", "Quality": 55.0})
_flagged = _adbe_row("FLAGGED", **{"Ticker": "FLAGGED", "Share Count Flagged": True})
_info_fcf = _adbe_row("INFOFCF", **{"Ticker": "INFOFCF", "FCF Source": "info"})
scan_store.save_scan("S&P 500", [_low_moat, _low_quality, _flagged, _info_fcf],
                      source_label="test fixture")
_best_excl, _ = te._build_best_by_ticker(log=lambda *a, **k: None, quality_exception_override=True)
check("3x row with Moat=60 (<80) stays excluded even with switch forced on",
      "LOWMOAT" not in _best_excl)
check("3x row with Quality=55 (<70) stays excluded even with switch forced on",
      "LOWQ" not in _best_excl)
check("Share-count-flagged 3x row stays excluded even with switch forced on",
      "FLAGGED" not in _best_excl)
check("info-FCF 3x row stays excluded even with switch forced on",
      "INFOFCF" not in _best_excl)
print("[task37_check5_still_excluded] every named exclusion case (low moat, "
      "low quality, share-count-flagged, info-FCF) stays excluded even with "
      "the exception forced on OK")


# ======================================================================
# CHECK 6: select_top100_pool() with the REAL env var - unset -> ADBE-
# shaped row never reaches the live, persisted pool; setting "1" (test-
# only, inside THIS test process, never on Railway) admits it into the
# real selection too, proving the gate actually reaches the live path.
# ======================================================================
if os.path.exists(ts.DB_PATH):
    os.remove(ts.DB_PATH)
scan_store.save_scan("S&P 500", _clean_rows + [_adbe_row()], source_label="test fixture")
os.environ.pop("TOP200_QUALITY_EXCEPTION_LIVE", None)
_pool_off = te.select_top100_pool(log=lambda *a, **k: None)
check("select_top100_pool(): switch OFF -> ADBE not in the real live pool",
      "ADBE" not in {r["ticker"] for r in _pool_off})

if os.path.exists(ts.DB_PATH):
    os.remove(ts.DB_PATH)
scan_store.save_scan("S&P 500", _clean_rows + [_adbe_row()], source_label="test fixture")
os.environ["TOP200_QUALITY_EXCEPTION_LIVE"] = "1"
_pool_on = te.select_top100_pool(log=lambda *a, **k: None)
os.environ.pop("TOP200_QUALITY_EXCEPTION_LIVE", None)
check("select_top100_pool(): switch ON -> ADBE IS in the real live pool",
      "ADBE" in {r["ticker"] for r in _pool_on})
_adbe_pool_row = next(r for r in _pool_on if r["ticker"] == "ADBE")
check("select_top100_pool(): ADBE's persisted row carries "
      "quality_exception_admitted=True", bool(_adbe_pool_row.get("quality_exception_admitted")))
print("[task37_check6_live_selection] the real TOP200_QUALITY_EXCEPTION_LIVE "
      "env var actually reaches select_top100_pool()'s own live/persisted "
      "selection, in both directions OK")


# ======================================================================
# CHECK 7: top100_store round-trip - save_pool()/current_pool()/
# is_quality_exception_admitted() for the new column.
# ======================================================================
if os.path.exists(ts.DB_PATH):
    os.remove(ts.DB_PATH)
ts.save_pool([
    {"ticker": "ADBE", "company_name": "Adobe", "universe": "S&P 500",
     "value_score": 62.8, "mos_pct": 67.2, "price": 246.44, "intrinsic_value": 750.64,
     "currency": "USD", "quality_exception_admitted": True},
    {"ticker": "CLEANX", "company_name": "Clean Co", "universe": "S&P 500",
     "value_score": 55.0, "mos_pct": 10.0, "price": 100.0, "intrinsic_value": 110.0,
     "currency": "USD", "quality_exception_admitted": False},
], "2026-11-06")
_pool = ts.current_pool()
_by_ticker = {r["ticker"]: r for r in _pool}
check("store round-trip: ADBE's quality_exception_admitted reads back truthy",
      bool(_by_ticker["ADBE"]["quality_exception_admitted"]))
check("store round-trip: CLEANX's quality_exception_admitted reads back falsy",
      not _by_ticker["CLEANX"]["quality_exception_admitted"])
check("is_quality_exception_admitted('ADBE') -> True",
      ts.is_quality_exception_admitted("ADBE") is True)
check("is_quality_exception_admitted('CLEANX') -> False",
      ts.is_quality_exception_admitted("CLEANX") is False)
check("is_quality_exception_admitted('NOPE') (not in the pool at all) -> False, "
      "never raises", ts.is_quality_exception_admitted("NOPE") is False)
print("[task37_check7_store_roundtrip] top100_store.save_pool()/current_pool()/"
      "is_quality_exception_admitted() round-trip the new column correctly OK")


# ======================================================================
# CHECK 8: top100_render._quality_exception_chip_html() - EN/ES text,
# gated purely by the row's own stored flag (no second env read here);
# shown ALONGSIDE (never instead of) the DCF-unreliable warning chip.
# ======================================================================
_admitted_row = {
    "ticker": "ADBE", "price": 246.44, "intrinsic_value": 750.64,
    "quality_exception_admitted": True,
}
_clean_row = {
    "ticker": "CLEANX", "price": 100.0, "intrinsic_value": 110.0,
    "quality_exception_admitted": False,
}
_chip_en = tr._quality_exception_chip_html(_admitted_row, "en")
check(f"admitted row, EN -> chip shown (chip={_chip_en!r})",
      "Admitted" in _chip_en and "high quality" in _chip_en)
_chip_es = tr._quality_exception_chip_html(_admitted_row, "es")
check(f"admitted row, ES -> chip shown (chip={_chip_es!r})", "Admitida" in _chip_es)
check("non-admitted row -> no chip at all",
      tr._quality_exception_chip_html(_clean_row, "en") == "")
check("admitted row's own DCF-unreliable warning chip is STILL shown "
      "(the Director's own explicit 'keeps its warning visible' instruction)",
      resolver_engine.dcf_looks_unreliable(_admitted_row["intrinsic_value"], _admitted_row["price"])
      and tr._dcf_unreliable_chip_html(_admitted_row, "en") != "")
print("[task37_check8_chip_html] _quality_exception_chip_html() shows EN/ES "
      "text only when the stored flag is set, alongside the still-visible "
      "DCF-unreliable warning chip OK")


print(f"\n{passed} passed, {failed} failed")
sys.exit(0 if failed == 0 else 1)
