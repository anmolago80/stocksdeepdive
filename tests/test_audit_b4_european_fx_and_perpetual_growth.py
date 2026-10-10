"""Audit fix B4 (Fable findings V7 + V11, 10 Oct 2026, instruction_
combined_10oct.md PART B): "European/private markets only. Static FX
fallbacks (CHF,USD) and (SEK,USD), with USD->USD = 1.0. Explicit
perpetual-growth entries for EUR, CHF and SEK. Cite the source for
each number in the commit message. Do not use model estimates. If
you can't cite a source, leave the value out and report it."

What this task found, and what was actually done about each part
(see fcf_valuation_engine.py/capm_engine.py's own comments at each
site for the full citation):

V11 - PERPETUAL_GROWTH_BY_CCY (capm_engine.py) had no EUR/CHF/SEK
entries, so all three silently fell back to DEFAULT_PERPETUAL_GROWTH
(2.5%, AUD's own rate) instead of a currency-specific figure - the
exact same bug class Stage 1a already fixed for GBP/CAD.
  - EUR: 2.0%, cited to the European Central Bank's symmetric 2%
    medium-term HICP inflation target (reaffirmed in the ECB's July
    2021 monetary policy strategy review). ADDED.
  - SEK: 2.0%, cited to Sveriges Riksbank's inflation target of 2%
    per year (Sweden's monetary policy target since 1993, specified
    in CPIF terms since September 2017). ADDED.
  - CHF: the Swiss National Bank's own price-stability definition is
    an explicit CEILING ("inflation below 2% per annum"), not a
    single point target like every other entry in this table -
    picking a specific number below that ceiling would itself be a
    model estimate, which this task's instruction explicitly says not
    to do without a citable source. NOT ADDED - still falls back to
    DEFAULT_PERPETUAL_GROWTH, reported rather than guessed.

V7 - _FX_STATIC_FALLBACK (fcf_valuation_engine.py, used by fx_rate()
for reporting-currency -> trading-currency DCF conversion) had no
(CHF, "USD")/(SEK, "USD") entries, so a Swiss/Swedish DCF conversion
with no live Yahoo FX rate available falls through to "fx_unavailable"
(valuation withheld entirely).
  - (CHF, "USD") / (SEK, "USD"): NOT ADDED. A spot FX rate moves daily
    and this sandbox has no outbound network access to fetch or
    verify a current one; every existing entry in this table (and
    FX_TO_USD_APPROX's own CHF/SEK figures, added under an earlier,
    looser task) is explicitly labelled "approximate, unverified... a
    plausible recent rate" - exactly the model-estimate category this
    task's instruction says not to introduce here. Reported instead
    of guessed. No regression: behaviour for CHF/SEK is unchanged
    (still fx_unavailable with no live rate) - just not newly covered.
  - USD->USD = 1.0: this piece WAS added - it's a mathematical
    identity, not an estimate. fx_rate() itself already short-
    circuits a literal USD->USD call before reaching the static
    table (unchanged, proven below); _static_rate_to_usd("USD") - the
    internal helper the cross-rate-via-USD fallback uses - now
    answers 1.0 directly instead of None, for correctness/robustness
    even though every currency this table currently maps already has
    a direct/reciprocal entry (so this exact branch is not reachable
    with today's table contents - proven not to change behaviour for
    any currency this app currently uses).

Every check below also proves S&P 500 / ASX 200 output is untouched:
neither USD nor AUD (the only two currencies those universes trade
in) had their perpetual rate or FX fallback changed by any of this.

Run: python3 tests/test_audit_b4_european_fx_and_perpetual_growth.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import capm_engine
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
# CHECK V11: the two ADDED currency-specific perpetual-growth entries.
# ======================================================================
check("EUR perpetual growth is 2.0% (ECB symmetric HICP target)",
      capm_engine.PERPETUAL_GROWTH_BY_CCY.get("EUR") == 0.020)
check("SEK perpetual growth is 2.0% (Riksbank CPIF target)",
      capm_engine.PERPETUAL_GROWTH_BY_CCY.get("SEK") == 0.020)
check("resolve_perpetual_rate('EUR') resolves to 2.0%",
      capm_engine.resolve_perpetual_rate("EUR") == 0.02)
check("resolve_perpetual_rate('SEK') resolves to 2.0%",
      capm_engine.resolve_perpetual_rate("SEK") == 0.02)

# CHF deliberately left OUT - no citable point-target source - still
# falls back to DEFAULT_PERPETUAL_GROWTH, exactly as before this fix.
check("CHF has NO dedicated entry (no citable point target, per this "
      "task's own 'leave it out and report it' instruction)",
      "CHF" not in capm_engine.PERPETUAL_GROWTH_BY_CCY)
check("resolve_perpetual_rate('CHF') still falls back to "
      "DEFAULT_PERPETUAL_GROWTH, unchanged by this fix",
      capm_engine.resolve_perpetual_rate("CHF") == capm_engine.DEFAULT_PERPETUAL_GROWTH)

# S&P 500 / ASX 200 (USD / AUD) completely unaffected.
check("USD perpetual growth is still exactly 2.0% (unchanged)",
      capm_engine.PERPETUAL_GROWTH_BY_CCY.get("USD") == 0.020)
check("AUD perpetual growth is still exactly 2.5% (unchanged)",
      capm_engine.PERPETUAL_GROWTH_BY_CCY.get("AUD") == 0.025)
check("resolve_perpetual_rate('USD') unchanged", capm_engine.resolve_perpetual_rate("USD") == 0.02)
check("resolve_perpetual_rate('AUD') unchanged", capm_engine.resolve_perpetual_rate("AUD") == 0.025)
# Every other pre-existing currency-keyed entry is untouched too.
check("GBP/CAD/JPY entries are byte-identical to before this fix",
      capm_engine.PERPETUAL_GROWTH_BY_CCY.get("GBP") == 0.020
      and capm_engine.PERPETUAL_GROWTH_BY_CCY.get("CAD") == 0.020
      and capm_engine.PERPETUAL_GROWTH_BY_CCY.get("JPY") == 0.010)


# ======================================================================
# CHECK V7: the static FX fallback table - CHF/SEK deliberately absent
# (reported, not guessed), USD identity fixed.
# ======================================================================
check("(CHF, 'USD') is NOT in _FX_STATIC_FALLBACK - no citable live "
      "spot rate available from this sandbox",
      ("CHF", "USD") not in fve._FX_STATIC_FALLBACK)
check("(SEK, 'USD') is NOT in _FX_STATIC_FALLBACK - same reason",
      ("SEK", "USD") not in fve._FX_STATIC_FALLBACK)
check("_static_rate_to_usd('USD') now answers 1.0 (identity, not an "
      "estimate) instead of None",
      fve._static_rate_to_usd("USD") == 1.0)

# fx_rate() itself: a direct USD->USD call was ALREADY 1.0 before this
# fix (the function's own same-currency short-circuit) - still is.
_rate, _source = fve.fx_rate("USD", "USD", log=None)
check("fx_rate('USD','USD') is still exactly (1.0, 'live') - unchanged",
      _rate == 1.0 and _source == "live")

# A Swiss/Swedish pair with no live rate (this sandbox has none) still
# correctly reports "unavailable" rather than inventing a number - no
# regression, not newly covered either.
_chf_rate, _chf_source = fve.fx_rate("CHF", "USD", log=None)
check("fx_rate('CHF','USD') with no live rate available still reports "
      "'unavailable' (None) - behaviour unchanged by this fix",
      _chf_rate is None and _chf_source == "unavailable")
_sek_rate, _sek_source = fve.fx_rate("SEK", "USD", log=None)
check("fx_rate('SEK','USD') with no live rate available still reports "
      "'unavailable' (None) - behaviour unchanged by this fix",
      _sek_rate is None and _sek_source == "unavailable")

# S&P 500 / ASX 200 (USD / AUD) FX fallback entries completely
# untouched - byte-identical to before this fix.
check("(USD,'AUD')/(AUD,'USD') fallback entries are unchanged",
      fve._FX_STATIC_FALLBACK.get(("USD", "AUD")) == 1.52
      and fve._FX_STATIC_FALLBACK.get(("AUD", "USD")) == 0.66)
_aud_rate, _aud_source = fve.fx_rate("AUD", "USD", log=None)
check("fx_rate('AUD','USD') (no live rate in this sandbox) still falls "
      "back to the pre-existing static 0.66, unchanged",
      _aud_rate == 0.66 and _aud_source == "fallback")

print()
print(f"PASS={passed} FAIL={failed}")
sys.exit(1 if failed else 0)
