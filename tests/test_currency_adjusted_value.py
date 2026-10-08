"""
Director addition to PUSH 2 (8 Oct 2026, delivered with the PUSH 1
go-ahead message, replacing any earlier home-currency-figures request):
"In the Deep Dive currency note and the Currency Risk 'margin of
safety, {home} view' table, add the currency-adjusted value in the
STOCK'S OWN trading currency after each margin of safety."

adjusted value = intrinsic value x (scenario rate / today's rate),
oriented so that margin = 1 - price / adjusted value reproduces each
percentage already shown, exactly. Price stays in the trading currency,
never converted. Implemented as currency_view_engine.adjusted_value(
price, margin_pct) = price / (1 - margin_pct/100) - algebraically the
same figure (see that function's own docstring for the proof), needing
only `price` and the margin already computed, so it can never drift
from the percentage sitting next to it.

Covers exactly the task's own listed test, production-shaped (CPRT-
like): intrinsic value 36.05 USD, price 26.62 USD, USD per AUD today
0.6967 / average 0.7031 / +1 sigma 0.7492 / -1 sigma 0.6570 - must give
about USD 35.72, 33.52, 38.23 and margins 25.5%, 20.6%, 30.4%.

Also covers: the Deep Dive currency note shows the same three adjusted
values; the Currency Risk table shows each scenario row's own adjusted
value; neither surface's strings contain a literal "$"; EN and ES are
genuinely distinct translations; "currency-adjusted value" label and
the one explanatory line both appear.

Run: python3 tests/test_currency_adjusted_value.py
"""
import os
import sys
import tempfile
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TESTVOL = tempfile.mkdtemp(prefix="currency_adjusted_value_test_")
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = TESTVOL

import account_currency_store as acs
import currency_risk_engine as cre
import currency_view_engine as cve
import i18n

if os.path.exists(acs.DB_PATH):
    os.remove(acs.DB_PATH)

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


from streamlit.testing.v1 import AppTest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# The CPRT-like fixture, verbatim from the Director's own instruction.
_IV = 36.05
_PRICE = 26.62
_MOS = (1.0 - _PRICE / _IV) * 100.0  # the stock's own starting margin, full precision
_FAKE_HISTORY = {"dates": ["2016-01-01", "2026-10-05"], "closes": [0.70, 0.6967], "stale": False}
_FAKE_STATS = {
    "today": 0.6967, "average": 0.7031, "sigma": 0.0461,
    "pct_vs_average": -0.9, "range_min": 0.6, "range_max": 0.8, "percentile": 50.0,
    "annualised_vol_pct": 5.0,
}

# ======================================================================
# CHECK 1: the pure-math helper, directly - the instruction's own
# worked numbers (margins already proven algebraically consistent;
# this checks the actual function).
# ======================================================================
with mock.patch.object(cre, "get_fx_history", return_value=_FAKE_HISTORY), \
     mock.patch.object(cre, "period_stats", return_value=_FAKE_STATS):
    _sc = cve.scenario_effects("AUD", "USD", position_size=1.0)
    _view = cve.mos_view(_MOS, "AUD", "USD")

check("scenario_effects() resolved for AUD/USD", _sc is not None)
check("mos_view() resolved for AUD/USD", _view is not None)

_adj_avg = cve.adjusted_value(_PRICE, _view["view_at_average"])
_adj_low = cve.adjusted_value(_PRICE, _view["view_low"])
_adj_high = cve.adjusted_value(_PRICE, _view["view_high"])
check(f"average-scenario adjusted value is about USD 35.72 (got {_adj_avg:.2f})",
      abs(_adj_avg - 35.72) < 0.05)
check(f"average-scenario margin is about 25.5% (got {_view['view_at_average']:.1f}%)",
      abs(_view["view_at_average"] - 25.5) < 0.15)
check(f"low-scenario (+1sigma) adjusted value is about USD 33.52 (got {_adj_low:.2f})",
      abs(_adj_low - 33.52) < 0.05)
check(f"low-scenario margin is about 20.6% (got {_view['view_low']:.1f}%)",
      abs(_view["view_low"] - 20.6) < 0.15)
check(f"high-scenario (-1sigma) adjusted value is about USD 38.23 (got {_adj_high:.2f})",
      abs(_adj_high - 38.23) < 0.05)
check(f"high-scenario margin is about 30.4% (got {_view['view_high']:.1f}%)",
      abs(_view["view_high"] - 30.4) < 0.15)
# The algebraic identity itself, exactly (never approximate): margin =
# 1 - price/adjusted_value must reproduce the SAME margin to full float
# precision, not just "about" - this is the "reproduces the percentage
# already shown, exactly" requirement, checked independently of rounding.
for _label, _margin, _adj in (
    ("average", _view["view_at_average"], _adj_avg),
    ("low", _view["view_low"], _adj_low),
    ("high", _view["view_high"], _adj_high),
):
    _reproduced = (1.0 - _PRICE / _adj) * 100.0
    check(f"{_label}: margin = 1 - price/adjusted_value reproduces the shown "
          f"percentage exactly (shown {_margin:.6f}, reproduced {_reproduced:.6f})",
          abs(_reproduced - _margin) < 1e-9)
check("adjusted_value(None, 25.0) returns None (no price to anchor to)",
      cve.adjusted_value(None, 25.0) is None)
check("adjusted_value(100.0, 100.0) returns None (degenerate, never invented)",
      cve.adjusted_value(100.0, 100.0) is None)
print("[adjusted_value_math] the instruction's own worked CPRT-like example matches "
      "(USD 35.72/33.52/38.23, margins 25.5%/20.6%/30.4%) and the margin-reproduction "
      "identity holds to full float precision OK")


# ======================================================================
# CHECK 2: the Deep Dive currency note shows all three adjusted values
# and the explainer line, with the stock's own currency code - no "$".
# ======================================================================
acs.set_home_currency("cadjnote@example.com", "AUD")
os.environ["CURRENCY_VIEW_LIVE"] = "1"  # display case under test, not the switch itself
_script2 = f"""
import os, sys
sys.path.insert(0, {REPO_ROOT!r})
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = {TESTVOL!r}
import streamlit as st
import paywall_engine as pw
pw.current_user_email = lambda: "cadjnote@example.com"
import app
_dd = {{"ticker": "CPRT", "mos": {_MOS!r}, "currency": "USD", "price": {_PRICE!r}}}
app._render_currency_note(_dd)
"""
with mock.patch.object(cre, "get_fx_history", return_value=_FAKE_HISTORY), \
     mock.patch.object(cre, "period_stats", return_value=_FAKE_STATS):
    _at2 = AppTest.from_string(_script2, default_timeout=60)
    _at2.run()
assert not _at2.exception, f"_render_currency_note() raised: {_at2.exception}"
_text2 = "\n".join(getattr(c, "value", "") or "" for c in _at2.caption)
check("Deep Dive note: label 'currency-adjusted value' appears", "currency-adjusted value" in _text2)
check("Deep Dive note: average adjusted value (USD 35.72) appears", "USD 35.72" in _text2)
check("Deep Dive note: low adjusted value (USD 33.52) appears", "USD 33.52" in _text2)
check("Deep Dive note: high adjusted value (USD 38.23) appears", "USD 38.23" in _text2)
check("Deep Dive note: the one explanatory line appears",
      "What the estimate is worth to a AUD investor" in _text2)
check("Deep Dive note: no literal '$' anywhere in its text", "$" not in _text2)
print("[deep_dive_note_adjusted_values] all three currency-adjusted values, the label and "
      "the explainer line all render, with USD codes only, no dollar sign OK")


# ======================================================================
# CHECK 3: the Currency Risk "margin of safety, {base} view" table
# shows each scenario row's OWN adjusted value - average/+1sigma/
# -1sigma rows individually, not just min/max.
# ======================================================================
_script3 = f"""
import os, sys
sys.path.insert(0, {REPO_ROOT!r})
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = {TESTVOL!r}
import currency_risk_render as crr
crr._render_mos_view_table("AUD", "USD", "CPRT", "en", is_owner=False,
                            ticker_currency="USD", mos={_MOS!r}, price={_PRICE!r})
"""
with mock.patch.object(cre, "get_fx_history", return_value=_FAKE_HISTORY), \
     mock.patch.object(cre, "period_stats", return_value=_FAKE_STATS):
    _at3 = AppTest.from_string(_script3, default_timeout=60)
    _at3.run()
assert not _at3.exception, f"_render_mos_view_table() raised: {_at3.exception}"

_rows3 = []
for df in _at3.get("dataframe"):
    try:
        _rows3 = df.value
    except Exception:
        pass
_col_adj = i18n.t("currency_risk.mos_view_col_adjusted_value", "en")
_col_margin = i18n.t("currency_risk.mos_view_col_margin", "en")
_adj_values_3 = list(_rows3.get(_col_adj, [])) if hasattr(_rows3, "get") else []
check("the table has a 'Currency-adjusted value' column with 3 rows",
      len(_adj_values_3) == 3)
check("the average row shows USD 35.72", any("USD 35.72" in v for v in _adj_values_3))
check("the +1sigma row shows USD 33.52", any("USD 33.52" in v for v in _adj_values_3))
check("the -1sigma row shows USD 38.23", any("USD 38.23" in v for v in _adj_values_3))

_captions3 = " ".join(c.value or "" for c in _at3.get("caption"))
check("the table's own explainer caption appears", "What the estimate is worth to a AUD investor" in _captions3)
check("no literal '$' anywhere in the table's captions", "$" not in _captions3)
print("[currency_risk_table_adjusted_values] each of the three scenario rows carries its "
      "own currency-adjusted value (USD 35.72/33.52/38.23), the explainer caption renders, "
      "no dollar sign OK")

# ======================================================================
# CHECK 4: three-letter codes only, never a symbol - no "£"/"A$"/"C$"
# anywhere either (not just a missing "$").
# ======================================================================
check("no currency SYMBOL characters anywhere in either surface's text (codes only)",
      not any(sym in (_text2 + _captions3) for sym in ("£", "¥", "A$", "C$")))

# ======================================================================
# CHECK 5: EN and ES strings are genuinely distinct translations.
# ======================================================================
_en_full = i18n.t("dd.currency_note.full", "en", home="AUD", stock="USD",
                   avg="25.5", low="20.6", high="30.4",
                   adj_avg="35.72", adj_low="33.52", adj_high="38.23")
_es_full = i18n.t("dd.currency_note.full", "es", home="AUD", stock="USD",
                   avg="25.5", low="20.6", high="30.4",
                   adj_avg="35.72", adj_low="33.52", adj_high="38.23")
check("dd.currency_note.full: EN and ES are distinct", _en_full != _es_full)
check("dd.currency_note.full: both carry the same adjusted-value figures",
      "35.72" in _en_full and "35.72" in _es_full
      and "33.52" in _en_full and "33.52" in _es_full
      and "38.23" in _en_full and "38.23" in _es_full)
check("dd.currency_note.full: neither language string contains '$'",
      "$" not in _en_full and "$" not in _es_full)

_en_explainer = i18n.t("dd.currency_note.adjusted_value_explainer", "en", home="AUD", stock="USD")
_es_explainer = i18n.t("dd.currency_note.adjusted_value_explainer", "es", home="AUD", stock="USD")
check("dd.currency_note.adjusted_value_explainer: EN and ES are distinct", _en_explainer != _es_explainer)

_en_col = i18n.t("currency_risk.mos_view_col_adjusted_value", "en")
_es_col = i18n.t("currency_risk.mos_view_col_adjusted_value", "es")
check("currency_risk.mos_view_col_adjusted_value: EN and ES are distinct", _en_col != _es_col)

_en_cr_explainer = i18n.t("currency_risk.mos_view_adjusted_value_explainer", "en", home="AUD", stock="USD")
_es_cr_explainer = i18n.t("currency_risk.mos_view_adjusted_value_explainer", "es", home="AUD", stock="USD")
check("currency_risk.mos_view_adjusted_value_explainer: EN and ES are distinct",
      _en_cr_explainer != _es_cr_explainer)
print("[i18n_distinct_translations] every new EN/ES string pair is genuinely distinct, "
      "carries the same figures, and no string contains '$' OK")

print()
print(f"PASS={passed} FAIL={failed}")
if os.path.exists(acs.DB_PATH):
    os.remove(acs.DB_PATH)
sys.exit(1 if failed else 0)
