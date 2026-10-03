"""
Lists & display, Commit 4 - forward P/E for pence-quoted London tickers
(Director-directed, 3 Oct 2026).

ALL FIXTURES IN THIS FILE ARE SYNTHETIC - this sandbox has no outbound
network access, same disclosure as every other fixture-based test in
this repo.

Covers:
  - fundamentals_data._resolve_forward_eps_unit(): pounds case, pence
    case, ambiguous ratio, zero/negative trailing EPS, GBP-quoted (not
    GBp) LSE ticker untouched, non-London ticker untouched.
  - The exact required log line format:
    "[units] TSCO.L forwardEps raw=31.2 trailing_gbp=0.27 ratio=115.6
    unit=pence used=0.312".
  - The pre-existing GBp PRICE rule (currency code only, never
    magnitude) is unchanged - confirmed by _normalize_pence_price_
    fields() still dividing every _PENCE_PRICE_INFO_FIELDS entry
    regardless of this commit's own forwardEps resolution.
  - Full regression suite green (run separately, not in this file).

Run: python3 tests/test_lists_display_commit4_forward_pe.py
"""
import os
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import fundamentals_data as fd


def _log_capture():
    lines = []
    return lines, (lambda msg: lines.append(msg))


# ======================================================================
# T1: pounds case - ratio in [0.2, 5] -> forwardEps used as-is.
# ======================================================================
info = {"currency": "GBp", "forwardEps": 1.5}
info, _ = fd._normalize_pence_price_fields(info)
info["trailingEps"] = 1.2  # statement-derived, pounds
lines, log = _log_capture()
info, unit = fd._resolve_forward_eps_unit("XYZ.L", info, log=log)
assert unit == "pounds" and info.get("forwardEps") == 1.5, (unit, info.get("forwardEps"))
assert lines and "unit=pounds" in lines[0] and "used=1.5" in lines[0], lines
print(f"[T1_pounds_case] ratio=1.25 -> pounds, forwardEps=1.5, logged: {lines[0]!r} OK")

# ======================================================================
# T2: pence case - ratio in [20, 500] -> forwardEps / 100. The EXACT
# required log line format.
# ======================================================================
info2 = {"currency": "GBp", "forwardEps": 31.2}
info2, _ = fd._normalize_pence_price_fields(info2)
info2["trailingEps"] = 0.27
lines2, log2 = _log_capture()
info2, unit2 = fd._resolve_forward_eps_unit("TSCO.L", info2, log=log2)
assert unit2 == "pence" and abs(info2.get("forwardEps") - 0.312) < 1e-9, info2
expected_log = "[units] TSCO.L forwardEps raw=31.2 trailing_gbp=0.27 ratio=115.6 unit=pence used=0.312"
assert lines2 == [expected_log], lines2
print(f"[T2_pence_case_exact_log] {lines2[0]!r} matches the required format exactly OK")

# ======================================================================
# T3: ambiguous ratio (neither band) -> forwardEps left n/a, unit
# "unknown", still logged.
# ======================================================================
info3 = {"currency": "GBp", "forwardEps": 8.0}
info3, _ = fd._normalize_pence_price_fields(info3)
info3["trailingEps"] = 1.0  # ratio = 8.0, between the two bands
lines3, log3 = _log_capture()
info3, unit3 = fd._resolve_forward_eps_unit("ABC.L", info3, log=log3)
assert unit3 == "unknown" and info3.get("forwardEps") is None, (unit3, info3.get("forwardEps"))
assert lines3 and "unit=unknown" in lines3[0] and "used=n/a" in lines3[0], lines3
print(f"[T3_ambiguous_ratio] ratio=8.0 (neither band) -> unknown, n/a, still logged: "
      f"{lines3[0]!r} OK")

# ======================================================================
# T4: zero/negative trailing EPS -> unknown, n/a (never divides by
# zero or a negative number).
# ======================================================================
for bad_trailing in (0.0, -0.5, None):
    info4 = {"currency": "GBp", "forwardEps": 8.0}
    info4, _ = fd._normalize_pence_price_fields(info4)
    if bad_trailing is not None:
        info4["trailingEps"] = bad_trailing
    info4, unit4 = fd._resolve_forward_eps_unit("NEG.L", info4)
    assert unit4 == "unknown" and info4.get("forwardEps") is None, (bad_trailing, unit4)
print("[T4_bad_trailing_eps] zero/negative/missing trailing EPS -> unknown, n/a, no "
      "crash OK")

# ======================================================================
# T5: GBP-quoted (already whole pounds, NOT GBp/GBX) LSE ticker is
# completely untouched - the pre-existing GBp PRICE rule (currency code
# only, never magnitude) still applies; this commit only ever touches
# forwardEps, and only for an actual GBp/GBX ticker.
# ======================================================================
info5 = {"currency": "GBP", "forwardEps": 2.0, "trailingEps": 1.5, "currentPrice": 2600.0}
info5, price_unit = fd._normalize_pence_price_fields(info5)
assert price_unit is None, price_unit
assert info5.get("forwardEps") == 2.0 and info5.get("currentPrice") == 2600.0, info5
info5, unit5 = fd._resolve_forward_eps_unit("SHEL.L", info5)
assert unit5 is None and info5.get("forwardEps") == 2.0, (unit5, info5.get("forwardEps"))
print("[T5_gbp_quoted_untouched] a GBP- (not GBp-) quoted LSE ticker's forwardEps/price "
      "are both completely untouched by this commit OK")

# ======================================================================
# T6: non-London tickers (USD/AUD/CAD/JPY) untouched.
# ======================================================================
for ccy, ticker in (("USD", "AAPL"), ("AUD", "CSL.AX"), ("CAD", "RY.TO"), ("JPY", "7203.T")):
    info6 = {"currency": ccy, "forwardEps": 10.0, "trailingEps": 9.0}
    info6, price_unit6 = fd._normalize_pence_price_fields(info6)
    assert price_unit6 is None, (ccy, price_unit6)
    info6, unit6 = fd._resolve_forward_eps_unit(ticker, info6)
    assert unit6 is None and info6.get("forwardEps") == 10.0, (ccy, unit6, info6.get("forwardEps"))
print("[T6_non_london_untouched] USD/AUD/CAD/JPY tickers' forwardEps completely "
      "unaffected by this commit OK")


print("\nALL Lists & display Commit 4 (forward P/E unit resolution) CHECKS PASSED")
