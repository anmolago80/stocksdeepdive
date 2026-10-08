"""
PART 2 STEP 2.2 of instruction_health_fixes_chart_and_new_markets.md
(8 Oct 2026, Director-directed): "'Margin of safety, AUD view (INTU)'
lists three results but not the figure they start from. Add one line
directly under the heading: the stock's own margin of safety in its
trading currency, the same number the Deep Dive shows."

currency_risk_render.py's _render_mos_view_table() now shows one
caption directly under the "Margin of safety, {base} view ({ticker})"
heading: the raw `mos` value (the SAME value - one source of truth,
from app.py's _resolve_ticker_mos_currency_live() - that the Deep Dive
note itself reads from the identical analyze() result), formatted
exactly like the Deep Dive note's own "+12.3%" style (a percentage,
never a currency amount - no dollar sign).

Covers:
  - the new line renders with the correct raw MOS figure, formatted
    {mos:+.1f}%, for a mos value that is NOT the table's own computed
    "resulting margin" (proving it shows the STARTING figure, not one
    of the three scenario results)
  - neither the EN nor the ES string contains a literal "$"
  - the EN and ES strings are distinct (a real translation, not a
    copy-paste)

Run: python3 tests/test_part2_step2_2_mos_starting_figure.py
"""
import os
import sys
import tempfile
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TESTVOL = tempfile.mkdtemp(prefix="part2_step2_2_test_")
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = TESTVOL

import currency_risk_engine as cre
import i18n

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


_FAKE_HISTORY = {"dates": ["2016-01-01", "2026-10-05"], "closes": [0.70, 0.6964], "stale": False}
_FAKE_STATS = {
    "today": 0.6964, "average": 0.7032, "sigma": 0.0461,
    "pct_vs_average": -1.0, "range_min": 0.6, "range_max": 0.8, "percentile": 50.0,
    "annualised_vol_pct": 5.0,
}
_MOS = 66.1  # deliberately NOT equal to any of the 3 scenario "resulting margin"
             # values the table itself computes, so the test can prove this new
             # line shows the STARTING figure, never one of those 3 results.

from streamlit.testing.v1 import AppTest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _run(lang):
    script = f"""
import os, sys
sys.path.insert(0, {REPO_ROOT!r})
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = {TESTVOL!r}
import currency_risk_render as crr
crr._render_mos_view_table("AUD", "USD", "INTU", {lang!r}, is_owner=False,
                            ticker_currency="USD", mos={_MOS!r})
"""
    with mock.patch.object(cre, "get_fx_history", return_value=_FAKE_HISTORY), \
         mock.patch.object(cre, "period_stats", return_value=_FAKE_STATS):
        at = AppTest.from_string(script, default_timeout=60)
        at.run()
    assert not at.exception, f"_render_mos_view_table() raised: {at.exception}"
    return at


_at_en = _run("en")
_captions_en = " ".join(c.value or "" for c in _at_en.get("caption"))
check("the EN starting-figure line is present, showing the exact raw MOS (+66.1%)",
      "+66.1%" in _captions_en)
check("the EN line names the ticker's own trading currency (USD)",
      "USD" in _captions_en)

_dataframe_margins = []
for df in _at_en.get("dataframe"):
    try:
        _dataframe_margins.extend(str(v) for v in df.value.get(
            i18n.t("currency_risk.mos_view_col_margin", "en"), []))
    except Exception:
        pass
check("+66.1% is NOT one of the table's own 3 computed 'resulting margin' values - "
      "proving the new line shows the STARTING figure, not a scenario result",
      "+66.1%" not in _dataframe_margins)

check("no literal '$' appears in the EN caption text (a margin of safety is a "
      "percentage, never a currency amount)",
      "$" not in _captions_en)

_at_es = _run("es")
_captions_es = " ".join(c.value or "" for c in _at_es.get("caption"))
check("the ES starting-figure line is present, showing the same raw MOS (+66.1%)",
      "+66.1%" in _captions_es)
check("no literal '$' appears in the ES caption text either",
      "$" not in _captions_es)

_en_line = i18n.t("currency_risk.mos_view_starting_figure", "en", ticker="INTU",
                   currency="USD", mos="+66.1%")
_es_line = i18n.t("currency_risk.mos_view_starting_figure", "es", ticker="INTU",
                   currency="USD", mos="+66.1%")
check("the EN and ES strings are genuinely distinct (a real translation)",
      _en_line != _es_line)
check("both EN and ES carry the exact same MOS figure embedded",
      "+66.1%" in _en_line and "+66.1%" in _es_line)

print()
print(f"PASS={passed} FAIL={failed}")
sys.exit(1 if failed else 0)
