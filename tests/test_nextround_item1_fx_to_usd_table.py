"""
Director's next round, item 1 (8 Oct 2026, instruction_health_fixes_
chart_and_new_markets.md): "FX-to-USD table used for the growth
ceiling and size premium: add SEK, CHF and every other trading
currency the site now holds; no silent .get(ccy, 1.0) fallback (log
and withhold instead)."

The bug: fcf_valuation_engine.FX_TO_USD_APPROX (growth ceiling/end
rate) and capm_engine._DISCOUNT_TIER_FX_TO_USD_APPROX (size premium)
were both missing SEK and CHF entirely, and capm_engine's own copy was
ALSO missing EUR (fcf_valuation_engine's copy already had it) - every
one of these gaps meant `.get(ccy, 1.0)` silently treated a non-USD
market cap as if it were already in USD, mis-sizing the company by
roughly the real FX rate (a SEK 630bn company read as a USD 630bn
mega-cap, not the ~USD 66bn large-cap it actually is).

Covers:
  - Both tables now carry all 8 trading currencies this site uses
    (USD/AUD/GBP/CAD/JPY/EUR/CHF/SEK), kept in sync by hand.
  - A Volvo-shaped SEK row (market cap SEK 630bn) gets the growth
    ceiling AND size premium for about USD 66bn, never USD 630bn -
    the exact fixture the Director's instruction named.
  - An unmapped currency (one this table genuinely doesn't know) is
    logged and WITHHELD - falls back to the existing size-unaware
    default (GROWTH_CEIL / DEFAULT_PERPETUAL_RATE / the micro-cap
    tier with market_cap_missing=True) - never silently multiplied
    by 1.0.
  - Before/after growth ceilings for the OMX Stockholm 30 fixture,
    printed as requested.

Run: python3 tests/test_nextround_item1_fx_to_usd_table.py
"""
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import capm_engine as ce
import fcf_valuation_engine as fve

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
# CHECK 1-4: both tables now carry all 8 currencies, kept in sync.
# ======================================================================
_ALL_CCY = {"USD", "AUD", "GBP", "CAD", "JPY", "EUR", "CHF", "SEK"}
check("fcf_valuation_engine.FX_TO_USD_APPROX has all 8 trading currencies",
      _ALL_CCY.issubset(fve.FX_TO_USD_APPROX.keys()))
check("capm_engine._DISCOUNT_TIER_FX_TO_USD_APPROX has all 8 trading currencies "
      "(EUR was previously missing from THIS table specifically)",
      _ALL_CCY.issubset(ce._DISCOUNT_TIER_FX_TO_USD_APPROX.keys()))
check("SEK is the SAME rate in both tables (kept in sync by hand)",
      fve.FX_TO_USD_APPROX["SEK"] == ce._DISCOUNT_TIER_FX_TO_USD_APPROX["SEK"])
check("CHF is the SAME rate in both tables",
      fve.FX_TO_USD_APPROX["CHF"] == ce._DISCOUNT_TIER_FX_TO_USD_APPROX["CHF"])

# ======================================================================
# CHECK 5-8: the Volvo-shaped SEK row - market cap SEK 630bn should
# bucket at roughly USD 66bn (large-cap), never USD 630bn (mega-cap).
# ======================================================================
_VOLVO_INFO = {"currency": "SEK", "marketCap": 630_000_000_000}
_volvo_mcap_usd = 630_000_000_000 * fve.FX_TO_USD_APPROX["SEK"]
check(f"Volvo-shaped SEK 630bn buckets to about USD {_volvo_mcap_usd/1e9:.1f}bn "
      "(the Director's own ~66bn figure)",
      60e9 <= _volvo_mcap_usd <= 72e9)

_ceiling_after = fve.growth_ceiling_for(_VOLVO_INFO)
_ceiling_if_mega = fve._log_interpolate(fve.GROWTH_CEILING_ANCHORS_USD, 630_000_000_000)
check("growth_ceiling_for() on the Volvo-shaped row does NOT equal the mega-cap-bucket "
      "ceiling the old 1.0-fallback bug would have produced",
      abs(_ceiling_after - _ceiling_if_mega) > 1e-6)

_rate_after, _meta_after = ce.resolve_discount_rate_by_market_cap(_VOLVO_INFO, "SEK")
check("resolve_discount_rate_by_market_cap() buckets the Volvo-shaped row at its real "
      "~USD 66bn size - market_cap_missing is False (item 2's own SEK risk-free-rate "
      "gap is a SEPARATE, expected reason `defaulted` may still be True here - not "
      "checked by this item)",
      _meta_after["market_cap_missing"] is False)
check(f"size-premium market_cap_usd stored on meta is about USD {_meta_after['market_cap_usd']/1e9:.1f}bn",
      60e9 <= _meta_after["market_cap_usd"] <= 72e9)

# ======================================================================
# CHECK 9-12: an UNMAPPED currency - logged, WITHHELD, never a silent
# 1.0 fallback.
# ======================================================================
_logged = []
_fake_log = logging.getLogger("sdd.growth")
_handler = logging.Handler()
_handler.emit = lambda record: _logged.append(record.getMessage())
_fake_log.addHandler(_handler)

_unmapped_info = {"currency": "ZZZ", "marketCap": 500_000_000_000}
_ceiling_unmapped = fve.growth_ceiling_for(_unmapped_info)
check("growth_ceiling_for() on an unmapped currency withholds the market-cap-aware "
      "result - falls back to the flat GROWTH_CEIL, never multiplies by 1.0",
      _ceiling_unmapped == fve.GROWTH_CEIL)
check("growth_ceiling_for() logged the unmapped currency",
      any("ZZZ" in m and "no FX_TO_USD_APPROX" in m for m in _logged))

_end_rate_unmapped = fve.growth_end_rate_for(_unmapped_info)
check("growth_end_rate_for() on an unmapped currency withholds too - falls back to "
      "DEFAULT_PERPETUAL_RATE",
      _end_rate_unmapped == fve.DEFAULT_PERPETUAL_RATE)

_rate_unmapped, _meta_unmapped = ce.resolve_discount_rate_by_market_cap(_unmapped_info, "ZZZ")
check("resolve_discount_rate_by_market_cap() on an unmapped currency withholds the size "
      "premium - market_cap_missing=True, defaulted=True, falls to the micro-cap tier "
      "(market_cap_usd=0), same as a genuinely missing market cap",
      _meta_unmapped["market_cap_missing"] is True and _meta_unmapped["defaulted"] is True
      and _meta_unmapped["market_cap_usd"] == 0)

_fake_log.removeHandler(_handler)

# ======================================================================
# Before/after growth ceilings for the OMX Stockholm 30 fixture, as
# the Director's own instruction asked to be printed.
# ======================================================================
print()
print("[OMX Stockholm 30 fixture] Volvo-shaped SEK 630bn market cap:")
_before = fve._log_interpolate(fve.GROWTH_CEILING_ANCHORS_USD, 630_000_000_000)
_after = fve.growth_ceiling_for(_VOLVO_INFO)
print(f"  BEFORE (the bug - SEK read as if it were already USD, 1.0 fallback): "
      f"market_cap_usd=${630_000_000_000/1e9:.0f}bn -> growth ceiling {_before:.4f} "
      f"({_before*100:.2f}%) - the MEGA-cap tier")
print(f"  AFTER  (this fix - SEK converted at {fve.FX_TO_USD_APPROX['SEK']}): "
      f"market_cap_usd=${_volvo_mcap_usd/1e9:.1f}bn -> growth ceiling {_after:.4f} "
      f"({_after*100:.2f}%) - the LARGE-cap tier")
check("BEFORE sits at the flat 8% mega-cap ceiling (the bug's own symptom)",
      abs(_before - 0.08) < 1e-6)
check("AFTER sits meaningfully above the mega-cap floor (correctly bucketed as large-cap, "
      "between the 12% and 8% anchors)",
      _after > 0.08 + 1e-6)

print()
print(f"PASS={passed} FAIL={failed}")
sys.exit(1 if failed else 0)
