"""
Director's overnight queue, TASK 3 (14 Oct 2026): "Preview: per-country
displacement." The preview's "Would displace" (Director's follow-up
queue TASK 2c, 14 Oct 2026, commit 791704c) only had "canada" and "all"
scenarios. This task adds ONE scenario per previewed country (UK,
France, Germany, Netherlands, Sweden, Switzerland), same read-back
method, so each country's own slot cost can be judged on its own.

Fix (top100_engine.py's preview_candidates_by_country(), generalized,
never a hand-maintained country-name list): "would_displace" is now
built from would_enter_counts.items() directly - one _pool_lowest_
members() scenario per ACTUAL previewed country, keyed by that
country's own name (the SAME key would_enter_counts/by_country already
use) - plus the fixed "all" catch-all. Any previewed country is
covered automatically; nothing here names UK/France/Germany/
Netherlands/Sweden/Switzerland specifically in the production code -
this file uses exactly those six (now that they're all public, per
the overnight queue's TASK 2) purely as its own test fixture choice,
to directly match the Director's own named example.

This file proves:
  a. All six named countries get their OWN "would displace" scenario,
     keyed by their own country name - never a shared "Europe" bucket.
  b. Each country's own scenario displaces exactly its own would-enter
     count's worth of the CURRENT pool's bottom members - read back,
     never resimulated, same _pool_lowest_members() every other
     scenario already uses.
  c. "all" still exists alongside every per-country scenario, and its
     own count is the SUM of every country's own count.
  d. A country with ZERO would-enter candidates still gets its own key
     (an empty list) - never silently dropped.
  e. select_top100_pool()'s own output is byte-identical before and
     after running the preview (TASK 3's additions are read-only).
  f. app._render_top200_preview_panel() renders one "If <Country> alone
     were added" block per previewed country, with no exception.

ALL FIXTURES IN THIS FILE ARE SYNTHETIC - this sandbox has no outbound
network access, same disclosure as every other fixture-based test in
this repo.

Run: python3 tests/test_director_overnight_task3_per_country_displacement.py
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TESTVOL = tempfile.mkdtemp(prefix="task3_per_country_displacement_test_")
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = TESTVOL
os.environ.pop("TOP200_PREVIEW_UNIVERSES", None)
os.environ.pop("PRIVATE_UNIVERSES", None)

import scan_store
import top100_engine as te
import top100_store

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


def _row(ticker, company_name, quality=55.0, mos_pct=16.7, psychology=5.0,
         discovery_lite=30.0, price=100.0, iv=120.0, long_score=60.0):
    return {
        "Ticker": ticker, "Company Name": company_name, "Price": price,
        "Intrinsic Value": iv, "MOS %": mos_pct, "Long Score": long_score,
        "Quality": quality, "Psychology": psychology, "Discovery (lite)": discovery_lite,
        "Moat": 50.0, "Growth Used": 0.05, "FCF Source": "reported",
        "Dividend Yield %": 2.0, "Is Fund": False, "price_unit_suspect": False,
        "Trading Status": None, "DCF Unreliable": False,
    }


# ======================================================================
# Seed a small CURRENT pool with a known cutoff - 7 members, so there's
# room for 6 countries' worth of individual displacement plus "all".
# ======================================================================
_hi_raw = _row("HI", "Hi Fixture", quality=95.0, mos_pct=80.0, psychology=9.0, discovery_lite=90.0)
_lo_raw = _row("LO", "Lo Fixture", quality=10.0, mos_pct=1.0, psychology=1.0, discovery_lite=5.0)
_hi_expected = te._recompute_value_score(_hi_raw)
_lo_expected = te._recompute_value_score(_lo_raw)
_cutoff = (_hi_expected + _lo_expected) / 2.0

top100_store.save_pool(
    [{"ticker": f"POOL{i}", "company_name": f"Pool {i}", "universe": "S&P 500",
      "value_score": _cutoff + (6 - i) * 10, "mos_pct": 5.0, "price": 100.0,
      "intrinsic_value": 105.0, "currency": "USD", "psychology": 5.0,
      "sector": "Industrials", "dividend_yield_pct": 1.0}
     for i in range(7)],
    as_of="2026-10-14",
)
check("sanity: the seeded pool's own lowest value_score (POOL6) equals the cutoff",
      te.pool_value_score_cutoff() == _cutoff)

# ======================================================================
# Six previewed countries - UK/France/Germany/Netherlands/Sweden/
# Switzerland (the Director's own named example, all public since the
# overnight queue's own TASK 2) - five with ONE above-cutoff candidate
# each (enters), one (Switzerland) with ZERO (never clears the cutoff),
# to prove a zero-count country still gets its own key.
_COUNTRY_FIXTURES = [
    ("UK.L", "United Kingdom", True),
    ("FR.PA", "France", True),
    ("DE.DE", "Germany", True),
    ("NL.AS", "Netherlands", True),
    ("SE.ST", "Sweden", True),
    ("CH.SW", "Switzerland", False),
]
_UNIVERSE_FOR = {
    "United Kingdom": "FTSE 100", "France": "CAC 40", "Germany": "DAX",
    "Netherlands": "AEX", "Sweden": "OMX Stockholm 30", "Switzerland": "SMI",
}
for _ticker, _country, _above in _COUNTRY_FIXTURES:
    _raw = _hi_raw if _above else _lo_raw
    _fixture_row = _row(_ticker, f"{_country} Fixture", quality=_raw["Quality"],
                        mos_pct=_raw["MOS %"], psychology=_raw["Psychology"],
                        discovery_lite=_raw["Discovery (lite)"])
    scan_store.save_scan(_UNIVERSE_FOR[_country], [_fixture_row], source_label="test fixture")

os.environ["TOP200_PREVIEW_UNIVERSES"] = ", ".join(_UNIVERSE_FOR.values())
_preview = te.preview_candidates_by_country()

# ======================================================================
# CHECK a-d: per-country displacement scenarios.
# ======================================================================
_would_displace = _preview["would_displace"]
check("TASK 3a: every one of the five above-cutoff countries has its own 'would_displace' key",
      all(c in _would_displace for c in
          ("United Kingdom", "France", "Germany", "Netherlands", "Sweden")))
check("TASK 3d: Switzerland (zero would-enter candidates) STILL has its own key, "
      "with an empty list - never silently dropped",
      "Switzerland" in _would_displace and _would_displace["Switzerland"] == [])
for _country in ("United Kingdom", "France", "Germany", "Netherlands", "Sweden"):
    _rows = _would_displace[_country]
    check(f"TASK 3b: {_country} alone displaces exactly 1 pool member (its own "
          "would-enter count), read back via _pool_lowest_members()",
          len(_rows) == 1)
check("TASK 3c: 'all' still exists alongside every per-country scenario, its own count "
      "is the SUM of all six countries' own would-enter counts (5, since Switzerland is 0)",
      "all" in _would_displace and len(_would_displace["all"]) == 5)
check("TASK 3: would_displace's own key set is EXACTLY the six previewed countries plus "
      "'all' - never a hand-picked subset, never a shared 'Europe' bucket",
      set(_would_displace.keys()) ==
      {"United Kingdom", "France", "Germany", "Netherlands", "Sweden", "Switzerland", "all"})

# ======================================================================
# CHECK e: select_top100_pool()'s own output is byte-identical before
# and after running the preview (TASK 3's additions are read-only).
# ======================================================================
_pool_before = te.select_top100_pool(log=lambda *a, **k: None)
te.preview_candidates_by_country()
_pool_after = te.select_top100_pool(log=lambda *a, **k: None)
check("select_top100_pool()'s own return value is byte-identical before and after "
      "running the per-country-displacement preview (TASK 3's additions are read-only)",
      json.dumps(_pool_before, sort_keys=True, default=str) ==
      json.dumps(_pool_after, sort_keys=True, default=str))

# ======================================================================
# CHECK f: app._render_top200_preview_panel() renders one "If <Country>
# alone were added" block per previewed country, no exception.
# ======================================================================
from streamlit.testing.v1 import AppTest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_universes_csv = os.environ["TOP200_PREVIEW_UNIVERSES"]
script = f"""
import os, sys
sys.path.insert(0, {REPO_ROOT!r})
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = {TESTVOL!r}
os.environ["TOP200_PREVIEW_UNIVERSES"] = {_universes_csv!r}
import app
app._render_top200_preview_panel()
"""
at = AppTest.from_string(script, default_timeout=60)
at.run()
check("app._render_top200_preview_panel() raises no exception with TASK 3's "
      "per-country displacement scenarios",
      not at.exception)
_markdown_text = " ".join(m.value or "" for m in at.get("markdown"))
for _country in ("United Kingdom", "France", "Germany", "Netherlands", "Sweden", "Switzerland"):
    check(f"the panel shows an 'If {_country} alone were added' block",
          f"If {_country} alone were added" in _markdown_text)
check("the panel still shows 'If every previewed country were added'",
      "If every previewed country were added" in _markdown_text)

os.environ.pop("TOP200_PREVIEW_UNIVERSES", None)

print()
print(f"PASS={passed} FAIL={failed}")
sys.exit(1 if failed else 0)
