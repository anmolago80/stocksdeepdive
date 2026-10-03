"""
Director addendum 2, Part 1 of 2, item B (3 Oct 2026) - dividend check
false alarms. Live log evidence, about 30 London tickers: "[units]
dividend_unit_suspect: implied yield 0.0070 vs Yahoo dividendYield
0.7000 - disagree by 99%" - Yahoo's dividendYield is a percent for
some tickers and a fraction for others; ~56 of 350 London companies
had dividends wrongly blanked. Same rules as the Lists & display
instruction. HOLD ALL PUSHES still applies.

ALL FIXTURES IN THIS FILE ARE SYNTHETIC - this sandbox has no outbound
network access, same disclosure as every other fixture-based test in
this repo.

Covers: fraction agrees, percent agrees, neither agrees, no Yahoo
yield, ticker in the log line (was missing), logged once per ticker
(was twice).

Run: python3 tests/test_director_addendum2_part1b_dividend_check.py
"""
import datetime as _dt
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import fundamentals_data as fd


def _div_fixture(dates, amounts, price, y_yield):
    return (
        {"dates": dates, "amounts": amounts},
        {"_price_quote_unit": "GBp", "currentPrice": price, "dividendYield": y_yield},
    )


_now = _dt.datetime.now(_dt.timezone.utc)
_d1 = (_now - _dt.timedelta(days=90)).isoformat()
_d2 = (_now - _dt.timedelta(days=270)).isoformat()


# ======================================================================
# B1: Yahoo's figure agrees AS A FRACTION (0.034 style) -> not suspect.
# ======================================================================
# TTM total (pence->pounds): (200+200)/100 = 4.0 pounds; price=100 pounds
# -> implied yield 0.04 (4%). Yahoo dividendYield=0.04 (already a
# fraction) agrees as-is.
divs, info = _div_fixture([_d1, _d2], [200.0, 200.0], price=100.0, y_yield=0.04)
fd._DIVIDEND_SUSPECT_LOGGED_TICKERS.clear()
lines = []
out, suspect = fd._normalize_and_validate_pence_dividends(
    divs, info, ticker="FRAC.L", log=lambda m: lines.append(m))
assert suspect is False, (suspect, lines)
assert out["amounts"] == [2.0, 2.0], out
assert lines == [], lines
print("[B1_fraction_agrees] Yahoo dividendYield as a plain fraction (0.04) agrees "
      "with the 4% implied yield -> not suspect OK")


# ======================================================================
# B2: Yahoo's figure agrees AS A PERCENT (divide by 100) - the exact
# live-evidence shape: implied 0.0070, Yahoo dividendYield=0.7000
# (0.70%, i.e. 0.70/100 = 0.0070) - must now agree, not disagree by 99%.
# ======================================================================
# TTM total (pence->pounds): 0.70 pounds; price=100 -> implied 0.0070.
divs2, info2 = _div_fixture([_d1, _d2], [35.0, 35.0], price=100.0, y_yield=0.70)
fd._DIVIDEND_SUSPECT_LOGGED_TICKERS.clear()
lines2 = []
out2, suspect2 = fd._normalize_and_validate_pence_dividends(
    divs2, info2, ticker="PCT.L", log=lambda m: lines2.append(m))
assert suspect2 is False, (suspect2, lines2)
assert lines2 == [], lines2
print("[B2_percent_agrees_live_evidence_shape] implied=0.0070 vs Yahoo "
      "dividendYield=0.7000 read as a percent (0.70/100=0.0070) -> agrees, "
      "the exact live false-alarm shape is now fixed OK")


# ======================================================================
# B3: neither reading agrees -> suspect=True, dividends cleared, ticker
# in the log line, logged once even if called twice for the same ticker
# (normalize_pence_quote() and get_bundle() can both validate the same
# ticker in one pipeline run - the old code logged twice).
# ======================================================================
# implied yield here: TTM (2000/100=20 pounds)/price(100) = 0.20 (20%).
# Yahoo dividendYield=5.0 -> as fraction 5.0 (2400% off), as percent
# 0.05 (5%, 75% off) - neither is within 20% of 0.20.
divs3, info3 = _div_fixture([_d1, _d2], [1000.0, 1000.0], price=100.0, y_yield=5.0)
fd._DIVIDEND_SUSPECT_LOGGED_TICKERS.clear()
lines3 = []
out3, suspect3 = fd._normalize_and_validate_pence_dividends(
    divs3, info3, ticker="TSCO.L", log=lambda m: lines3.append(m))
assert suspect3 is True, suspect3
assert out3 == {"dates": [], "amounts": []}, out3
assert len(lines3) == 1, lines3
assert "TSCO.L" in lines3[0], lines3[0]
assert "dividend_unit_suspect" in lines3[0], lines3[0]

out3b, suspect3b = fd._normalize_and_validate_pence_dividends(
    divs3, info3, ticker="TSCO.L", log=lambda m: lines3.append(m))
assert suspect3b is True
assert len(lines3) == 1, lines3
print(f"[B3_neither_agrees_once_per_ticker] {lines3[0]!r} - ticker in the line, "
      "suspect=True, dividends cleared, second call for the same ticker logs "
      "nothing more OK")


# ======================================================================
# B4: no Yahoo dividendYield at all -> can't validate, not suspect
# (never flagged, same "can't confirm != confirmed wrong" rule).
# ======================================================================
divs4, info4 = _div_fixture([_d1, _d2], [200.0, 200.0], price=100.0, y_yield=None)
fd._DIVIDEND_SUSPECT_LOGGED_TICKERS.clear()
lines4 = []
out4, suspect4 = fd._normalize_and_validate_pence_dividends(
    divs4, info4, ticker="NOYIELD.L", log=lambda m: lines4.append(m))
assert suspect4 is False, suspect4
assert out4["amounts"] == [2.0, 2.0], out4
assert lines4 == [], lines4
print("[B4_no_yahoo_yield] no info['dividendYield'] at all -> left as the plain "
      "/100-normalised amounts, not flagged, nothing logged OK")


print("\nALL Director addendum 2, Part 1, item B (dividend check false alarms) CHECKS PASSED")
