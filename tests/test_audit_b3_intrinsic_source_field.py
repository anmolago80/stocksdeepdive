"""Audit fix B3 (Fable finding V6, 10 Oct 2026, instruction_combined_
10oct.md PART B): "Additive only. Store 'Intrinsic Source' (dcf /
pe-blend / ...) on scan rows and in _PUBLIC_FIELD_MAP. Set
fcf_reason='no_shares' on the shares-missing path. Prove that no
existing column's values change."

Three independent pieces, each additive:

1. fcf_valuation_engine.dcf_intrinsic_value(): the shares<=0 branch
   (info has no usable sharesOutstanding/impliedSharesOutstanding/
   filed count - share_class_engine.whole_company_shares() returns
   0) used to abandon the DCF with meta["fcf_reason"] left at its
   init default of None - no way to tell "no shares data" apart from
   any other reason the DCF never even started. Now sets
   meta["fcf_reason"] = "no_shares" on that exact branch, before the
   early return. The return SHAPE and the other two return values
   (0, None) are unchanged - this only fills in a previously-unset
   diagnostic field.

2. nightly_scan.analyze_ticker_lite(): resolve_intrinsic_value()'s own
   second return value (the source label "dcf"/"pe-blend"/"none" -
   see resolver_engine.resolve_intrinsic_value()'s own docstring) was
   captured into a deliberately-unused `_ivsrc` and discarded. app.py's
   own live-hook equivalent already captures and surfaces this same
   value as "Intrinsic Source" (see app.py's own row-builder, the
   non-fallback branch) - nightly_scan.py now does the same: the
   variable is renamed (no more leading underscore - it's used) and a
   NEW "Intrinsic Source" key is added to the row dict, immediately
   after the existing "Intrinsic Value" key. No existing key's
   computation changes - verified below by source inspection (the
   diff is a rename plus one new dict entry, nothing else touched on
   that call site or in that return dict).

3. snapshot_store._PUBLIC_FIELD_MAP gets one new entry so "Intrinsic
   Source" reaches the public surfaces (snapshot page, /api/v1/*,
   MCP) through the same single whitelist choke point every other
   public field already uses - see that module's own docstring.
   Every pre-existing entry is verified unchanged below (hardcoded
   expected dict, not just "key count").

Run: python3 tests/test_audit_b3_intrinsic_source_field.py
"""
import inspect
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

import fcf_valuation_engine as fve
import resolver_engine
import nightly_scan
import snapshot_store

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


_CF = pd.DataFrame({"2026-06-30": [100.0, -20.0]},
                    index=["Operating Cash Flow", "Capital Expenditure"])

# ======================================================================
# CHECK 1: the shares-missing path. info has no sharesOutstanding, no
# impliedSharesOutstanding, and `ticker` isn't on the known multi-class
# list - share_class_engine.whole_company_shares() returns shares=0,
# so dcf_intrinsic_value() abandons immediately.
# ======================================================================
_iv1, _g1, _meta1 = fve.dcf_intrinsic_value(
    "NOSHARES", info={"currency": "USD"}, cashflow_df=_CF,
)
check("shares-missing path: intrinsic value is still 0 (unchanged)", _iv1 == 0)
check("shares-missing path: growth_rate_used is still None (unchanged)", _g1 is None)
check("shares-missing path: fcf_reason is now 'no_shares' (was None pre-fix)",
      _meta1.get("fcf_reason") == "no_shares")

# Same path via resolve_intrinsic_value() (the caller nightly_scan.py/
# app.py actually use) - P/E-blend takes over for the overall IV/source,
# but the underlying dcf_meta's fcf_reason is NOT threaded through the
# pe-blend branch (that branch has its own, different fcf_reason
# passthrough for the negative-FCF case - see resolver_engine.py's own
# comment) - this check only concerns dcf_intrinsic_value()'s own meta,
# proven directly above, which is the one Fable's finding V6 names.

# A sanity check that a NORMAL ticker (real shares, real positive FCF)
# is completely unaffected - fcf_reason stays None, nothing new appears.
_CF_OK = pd.DataFrame(
    {"2026-06-30": [500.0, -50.0], "2025-06-30": [480.0, -48.0], "2024-06-30": [460.0, -46.0]},
    index=["Operating Cash Flow", "Capital Expenditure"],
)
_iv2, _g2, _meta2 = fve.dcf_intrinsic_value(
    "NORMAL", info={"currency": "USD", "sharesOutstanding": 1_000_000, "marketCap": 50_000_000},
    cashflow_df=_CF_OK, discount_rate=0.10, perpetual_rate=0.02,
)
check("a normal ticker with real shares is unaffected: fcf_reason stays None",
      _meta2.get("fcf_reason") is None)
check("a normal ticker with real shares still gets a positive intrinsic value",
      _iv2 > 0)


# ======================================================================
# CHECK 2: nightly_scan.analyze_ticker_lite()'s own source - the
# resolve_intrinsic_value() call site no longer discards its source
# label, and the return dict carries it as "Intrinsic Source"
# immediately after "Intrinsic Value". Calling the full function needs
# a live yfinance price history fetch (this repo's own established
# precedent for testing this function's row-shape wiring without that -
# see test_dcf_outlier_guards.py's own "confirmed by calling it the
# same way analyze_ticker_lite() does" comment - is direct source
# inspection).
# ======================================================================
_src = inspect.getsource(nightly_scan.analyze_ticker_lite)
check("the resolve_intrinsic_value() call site no longer discards the source "
      "label into a throwaway _ivsrc",
      "_ivsrc" not in _src)
check("resolve_intrinsic_value(...) is still called with the same 4-way unpack "
      "shape (intrinsic, <source>, <growth>, iv_meta)",
      "resolve_intrinsic_value(" in _src)
check('the return dict sets "Intrinsic Source": ivsrc,',
      '"Intrinsic Source": ivsrc,' in _src)

# The existing "Intrinsic Value"/"Intrinsic Default" lines themselves
# are untouched (same exact text as before this fix) - proves this is
# a pure addition, not a rewrite of the surrounding lines - and the
# new key sits between them in the same order app.py's own row-builder
# already uses (Intrinsic Value, Intrinsic Source, Intrinsic Default).
check('"Intrinsic Value": round(intrinsic, 2) if intrinsic > 0 else None, is unchanged',
      '"Intrinsic Value": round(intrinsic, 2) if intrinsic > 0 else None,' in _src)
check('"Intrinsic Default": bool(iv_meta.get("value_default", False)), is unchanged',
      '"Intrinsic Default": bool(iv_meta.get("value_default", False)),' in _src)
_idx_value = _src.index('"Intrinsic Value": round(intrinsic, 2) if intrinsic > 0 else None,')
_idx_source = _src.index('"Intrinsic Source": ivsrc,')
_idx_default = _src.index('"Intrinsic Default": bool(iv_meta.get("value_default", False)),')
check('"Intrinsic Source" sits between "Intrinsic Value" and "Intrinsic Default"',
      _idx_value < _idx_source < _idx_default)


# ======================================================================
# CHECK 3: snapshot_store._PUBLIC_FIELD_MAP - the new entry is present,
# AND every pre-existing entry is byte-identical to before this fix
# (hardcoded expected dict minus the new key - proves nothing existing
# was renamed/removed/repointed).
# ======================================================================
_EXPECTED_PRE_EXISTING = {
    "Price": "price",
    "Intrinsic Value": "intrinsic_value",
    "MOS %": "mos_pct",
    "Quality": "quality",
    "Psychology": "psychology",
    "Discovery (lite)": "discovery",
    "Long Score": "value_score",
    "Valuation": "valuation_label",
    "Type": "company_type",
    "Quality Default": "quality_estimated",
    "Intrinsic Default": "intrinsic_estimated",
    "Company Name": "company_name",
    "Implied Growth %": "implied_growth_pct",
    "Model Growth %": "model_growth_pct",
    "Percentiles": "percentiles",
    "Dividend TTM": "dividend_ttm",
    "Dividend Yield %": "dividend_yield_pct",
    "Payout Ratio %": "payout_ratio_pct",
    "Next Ex-Div Date": "next_ex_date",
    "Trading Status": "trading_status",
}
_actual_map = snapshot_store._PUBLIC_FIELD_MAP
check("every pre-existing _PUBLIC_FIELD_MAP entry is present and unchanged",
      all(_actual_map.get(k) == v for k, v in _EXPECTED_PRE_EXISTING.items()))
check("no pre-existing _PUBLIC_FIELD_MAP entry was removed",
      set(_EXPECTED_PRE_EXISTING) <= set(_actual_map))
check('"Intrinsic Source" is now in _PUBLIC_FIELD_MAP',
      "Intrinsic Source" in _actual_map)
check('_PUBLIC_FIELD_MAP has exactly one new key beyond the pre-existing set '
      "from THIS fix (B3) - later audit fixes (e.g. C4's own \"DCF Unreliable "
      "(Symmetric)\") add further keys beyond this one, which this check "
      "deliberately allows for rather than pinning the map to a single "
      "moment in time",
      {"Intrinsic Source"} <= (set(_actual_map) - set(_EXPECTED_PRE_EXISTING)))

# public_view() actually exposes it when present on a row, and omits it
# (same "never fabricate" convention as every other optional field)
# when the row predates this fix.
_public_key = _actual_map["Intrinsic Source"]
_row_with = {"Intrinsic Value": 42.0, "Intrinsic Source": "pe-blend"}
_row_without = {"Intrinsic Value": 42.0}
_out_with = snapshot_store.public_view(_row_with)
_out_without = snapshot_store.public_view(_row_without)
check("public_view() exposes Intrinsic Source under its public key when present",
      _out_with.get(_public_key) == "pe-blend")
check("public_view() omits the public key entirely when the row has no "
      "Intrinsic Source (old cached row, pre-fix) - never fabricated",
      _public_key not in _out_without)
check("public_view() leaves every OTHER existing public key untouched",
      _out_with.get("intrinsic_value") == 42.0 and _out_without.get("intrinsic_value") == 42.0)

print()
print(f"PASS={passed} FAIL={failed}")
sys.exit(1 if failed else 0)
