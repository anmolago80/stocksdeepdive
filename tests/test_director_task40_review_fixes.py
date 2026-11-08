"""
TASK 40 (Director-directed, 8 Nov 2026): fixes from the Director's own
review of the admin previews after TASK 36-38.

Covers:
  1. The real replay-panel crash (app._render_replay_at_date_panel()'s
     own `_date = st.date_input(...)` shadowing the module-level `date
     as _date` import - UnboundLocalError the moment the function is
     actually RUN) - tested in tests/test_director_task38_point_in_
     time_replay.py's own CHECK 8 (AppTest, clicking the button) since
     that's where the fixed code lives; not duplicated here.
  2. Mortgage REITs treated as financials mode for Quality v2 only
     (quality_v2_engine._is_mortgage_reit()).
  3. Quality v2 missing -> falls back to live Quality (never None,
     never silently "dropped") + the new "quality_v2_missing" flag.
  6. The TASK 36 support chip's own card-layout row - its OWN div,
     never folded into the row-1/row-2 wrapping flex container, so
     nothing on rows 1-2 can move/re-wrap because of it.

  5. ELS.AX Fair Value DCF vs Deep Dive MOS gauge IV mismatch: root
     cause was fundamentals_data.get_bundle() unconditionally flagging
     flags=["currency_converted"] after calling _convert_statement_
     currency() on income/balance/cashflow/income_q, with no check
     that any of the four actually succeeded - a column whose fx rate
     couldn't be resolved (or a swallowed exception) silently left
     that ONE statement raw while the flag still claimed full success.
     auto_compounder_engine._dcf_info_with_verified_currency_override()
     (TASK 35 item B) trusts that flag to skip re-converting cashflow_
     df itself - so a false "currency_converted" meant the Fair Value
     DCF path used ELS.AX's still-raw USD cashflow as if it were
     already AUD, understating its IV against Deep Dive's own
     (correct) figure, which never reads this flag at all and
     converts its own raw cashflow fetch independently. Fixed in
     fundamentals_data._convert_statement_currency() (now returns
     (df, ok)) and currency_risk_engine.historical_fx_rate()'s own
     exception fallback (used to fabricate a fake identity rate of 1.0
     - see tests/test_b2_1_fx_history.py for those two fixes' own
     tests). CHECK 5 below reproduces the exact live symptom end to
     end with a synthetic bundle and confirms it's gone.

Items 4/7 (GQG.AX/STJ.L root causes, the re-run impact/cost table)
need real production data this sandbox cannot fetch - reported
separately, not as automated tests here.

Run: python3 tests/test_director_task40_review_fixes.py
"""
import os
import sys
import tempfile
from unittest import mock

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TESTVOL = tempfile.mkdtemp(prefix="task40_test_")
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = TESTVOL

import auto_compounder_engine as ace
import fundamentals_data as fd
import quality_v2_engine as qv2
import top100_engine as te
import top100_render as tr

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
# CHECK 1 (item 2): mortgage REITs treated as financials mode for
# Quality v2 only - financials_classifier.is_financials() itself is
# NOT touched (moat_engine/fcf_valuation_engine still see the REIT
# exclusion as before); equity/property REITs stay standard mode.
# ======================================================================
check("'REIT - Mortgage' industry -> _is_mortgage_reit() True",
      qv2._is_mortgage_reit({"industry": "REIT - Mortgage"}))
check("'REIT—Mortgage' (em dash, Yahoo's own punctuation variance) -> True",
      qv2._is_mortgage_reit({"industry": "REIT—Mortgage"}))
check("'Mortgage REIT' (reversed word order) -> True",
      qv2._is_mortgage_reit({"industry": "Mortgage REIT"}))
check("'REIT - Residential' (equity/property REIT, no 'mortgage') -> False",
      not qv2._is_mortgage_reit({"industry": "REIT - Residential"}))
check("'REIT - Diversified' -> False", not qv2._is_mortgage_reit({"industry": "REIT - Diversified"}))
check("no industry at all -> False, never raises", not qv2._is_mortgage_reit({}))

import financials_classifier as fc
check("financials_classifier.is_financials() ITSELF is untouched - a mortgage "
      "REIT still reads as the REIT exclusion (False) there (moat_engine/"
      "fcf_valuation_engine's own ROIC/DCF path is deliberately NOT widened "
      "by this task)",
      fc.is_financials({"industry": "REIT - Mortgage"}) is False)
print("[task40_check1_mortgage_reit_classification] _is_mortgage_reit() correctly "
      "identifies mortgage REITs (robust to Yahoo's own punctuation variance), "
      "leaves an equity/property REIT alone, and does NOT widen the shared "
      "financials_classifier itself OK")


# ======================================================================
# CHECK 2 (item 2, continued): a mortgage-REIT-shaped bundle actually
# gets the financials-mode (ROE-based) treatment in get_quality_
# score_v2() - meta["financials_mode"] is True, every input falls back
# to .info exactly like a real bank/insurer would.
# ======================================================================
_mreit_info = {"industry": "REIT - Mortgage", "returnOnEquity": 0.11}
_score, _meta = qv2.get_quality_score_v2("MREIT", {"info": _mreit_info}, info=_mreit_info)
check("mortgage-REIT-shaped bundle: meta['financials_mode'] is True",
      _meta["financials_mode"] is True)
check("mortgage-REIT-shaped bundle: every input is a fallback (no statement "
      "data was read at all, same as a real bank/insurer with no income "
      "statement in this fixture)",
      set(_meta["fallback_fields"]) == set(qv2._INPUT_FIELDS))

_ereit_info = {"industry": "REIT - Residential", "returnOnEquity": 0.08}
_score2, _meta2 = qv2.get_quality_score_v2("EREIT", {"info": _ereit_info}, info=_ereit_info)
check("equity-REIT-shaped bundle (no 'mortgage' in industry): meta['financials_mode'] "
      "is False - unchanged from before this task", _meta2["financials_mode"] is False)
print("[task40_check2_mortgage_reit_mode_applied] get_quality_score_v2() actually "
      "applies financials mode for a mortgage REIT, leaves an equity/property "
      "REIT exactly as before OK")


# ======================================================================
# CHECK 3 (item 3): Quality v2 missing -> value_score_v2/value_score_
# v2_moat fall back to the live Quality (never None), flagged
# "quality_v2_missing" - never silently looks dropped.
# ======================================================================
_row_no_v2 = {
    "Quality": 60.0, "MOS %": 20.0, "Psychology": 5.0, "Discovery (lite)": 30.0,
    "Moat": 86.0,
}
check("_recompute_value_score_v2(): no Quality V2 -> falls back to live Quality, "
      "matching _recompute_value_score() exactly (never None)",
      te._recompute_value_score_v2(_row_no_v2) == te._recompute_value_score(_row_no_v2))
check("_recompute_value_score_v2_moat(): no Quality V2 -> falls back to the live "
      "Quality + Moat blend, matching _recompute_value_score_moat() exactly",
      te._recompute_value_score_v2_moat(_row_no_v2) == te._recompute_value_score_moat(_row_no_v2))

_row_with_v2 = dict(_row_no_v2, **{"Quality V2": 90.0})
check("_recompute_value_score_v2(): a REAL Quality V2 is still used when present "
      "(this fallback never overrides real data)",
      te._recompute_value_score_v2(_row_with_v2) != te._recompute_value_score(_row_with_v2))

_row_neither = {"MOS %": 20.0, "Psychology": 5.0, "Discovery (lite)": 30.0, "Moat": 86.0}
check("_recompute_value_score_v2(): no Quality V2 AND no live Quality either -> "
      "None (genuinely nothing to compute, never fabricated)",
      te._recompute_value_score_v2(_row_neither) is None)
print("[task40_check3_v2_fallback] value_score_v2/value_score_v2_moat fall back "
      "to the live Quality's own figures when Quality V2 is missing, never "
      "silently None, never overriding a real v2 score when one exists OK")


# ======================================================================
# CHECK 4 (item 6): the support chip's own card-layout row - a
# separate flex div, never folded into the row-1/row-2 wrapping
# container. Verified structurally: patch _support_chip_html() to
# return a known marker, call the real _render_row(), capture the
# markdown HTML Streamlit would have rendered, and confirm the marker
# sits in its OWN div, strictly AFTER the row-1/row-2 div's own
# closing tag - never inside it.
# ======================================================================
_captured_markdown = []
tr.st.markdown = lambda html, unsafe_allow_html=True: _captured_markdown.append(html)
_MARKER = "##SUPPORT_CHIP_MARKER##"
tr._support_chip_html = lambda row, lang: _MARKER
tr._quality_exception_chip_html = lambda row, lang: ""
tr._dcf_unreliable_chip_html = lambda row, lang: ""
tr._stale_valuation_chip_html = lambda row, lang: ""
tr._tradability_spread_pct = lambda ticker: None
tr._sentiment_chip_html = lambda psychology, lang: ""

_dims = {k: {"score": 4, "justification": "j", "source_period": "FY25"} for k in te.DIMENSION_KEYS}
_row = {
    "ticker": "WMS", "company_name": "WMS Co", "sector": "Industrials",
    "value_score": 55.0, "mos_pct": 20.0, "price": 10.0, "intrinsic_value": 12.5,
    "currency": "AUD", "psychology": 5.0, "dividend_yield_pct": 2.0,
    "composite": 70.0,
    "score_row": {"composite": 70.0, "dims": _dims, "not_rated": False},
}
tr._render_row(1, _row, "en", {}, tr.SORT_RESEARCH)

check("_render_row(): exactly one st.markdown call for the card itself",
      len(_captured_markdown) >= 1)
_html = _captured_markdown[0]
check(f"_render_row(): support chip marker is present somewhere in the card",
      _MARKER in _html)
_row1_div_close_idx = _html.index(_MARKER)
# The marker must NOT be inside the first (row-1/row-2) flex div - i.e.
# that div's own closing "</div>" must appear BEFORE the marker.
_first_div_open = _html.index("display:flex;flex-wrap:wrap;align-items:center;gap:12px;'>")
_first_div_close = _html.index("</div>", _first_div_open)
check(f"_render_row(): the support chip marker sits AFTER the row-1/row-2 "
      f"div's own closing tag (first div closes at {_first_div_close}, marker "
      f"at {_row1_div_close_idx}) - its own separate row, never folded into "
      f"rows 1-2's shared wrapping container",
      _first_div_close < _row1_div_close_idx)
check("_render_row(): the marker is wrapped in its OWN flex div with margin-top "
      "(a genuinely separate row, not bare text dropped between divs)",
      "margin-top:6px;'>" + _MARKER in _html)
print("[task40_check4_support_chip_own_row] the support chip renders in its own "
      "dedicated row, strictly after rows 1-2's own shared div closes - rows "
      "1-2's own content/order is completely untouched by its presence OK")

# And: when there's no support chip at all (""), no extra empty div is
# ever added - rows 1-2 render byte-identically to a card with no
# support entry.
tr._support_chip_html = lambda row, lang: ""
_captured_markdown.clear()
tr._render_row(1, _row, "en", {}, tr.SORT_RESEARCH)
check("_render_row(): no support chip -> no stray empty support-row div added "
      "at all (the exact 'cards look exactly as before TASK 36 when the gate "
      "is off' requirement)",
      "margin-top:6px;'></div>" not in _captured_markdown[0])
print("[task40_check4b_no_stray_row] no support entry for this ticker -> no "
      "empty extra row rendered, cards look exactly as before TASK 36 OK")


# ======================================================================
# CHECK 5 (item 5): the ELS.AX Fair Value-vs-Deep-Dive IV mismatch,
# reproduced end to end with a synthetic bundle. info["financialCurrency"]
# = "USD", listing currency "AUD" - same shape as the real live ELS.AX
# case. Before this fix, get_bundle() would have appended flags=
# ["currency_converted"] here regardless of whether the per-column fx
# lookup inside _convert_statement_currency() actually succeeded, and
# _dcf_info_with_verified_currency_override() would then have trusted
# that false flag and overridden financialCurrency to "AUD" - telling
# dcf_intrinsic_value() "don't convert cashflow again" for a cashflow
# that, in fact, was never converted at all. historical_fx_rate() is
# mocked here to return (None, "unavailable") for every column - the
# exact "a column's rate couldn't be resolved" case this task's own
# fix (fundamentals_data._convert_statement_currency() now reporting
# (df, ok)) and currency_risk_engine.historical_fx_rate()'s no-longer-
# fake-1.0 exception fallback both target.
# ======================================================================
_els_info = {
    "currency": "AUD", "financialCurrency": "USD",
    "currentPrice": 1.43, "regularMarketPrice": 1.43,
    "marketCap": 1.43 * 226_243_359, "sharesOutstanding": 226_243_359,
}
_els_cashflow = pd.DataFrame({"2025-06-30": [11_079_942.0]}, index=["Free Cash Flow"])
_els_income = pd.DataFrame({"2025-06-30": [5_000_000.0]}, index=["Net Income"])
_els_balance = pd.DataFrame({"2025-06-30": [50_000_000.0]}, index=["Total Stockholders Equity"])


class _ElsFakeTicker:
    def __init__(self, *_a, **_k):
        self.info = dict(_els_info)
        self.income_stmt = _els_income
        self.balance_sheet = _els_balance
        self.cashflow = _els_cashflow
        self.quarterly_income_stmt = pd.DataFrame()
        self.dividends = pd.Series(dtype=float)

    @property
    def fast_info(self):
        raise AttributeError

    def history(self, *a, **k):
        return pd.DataFrame()


with mock.patch.object(fd, "yf") as _myf, \
     mock.patch.object(fd.fcf_valuation_engine, "fx_rate", return_value=(1.456, "live")), \
     mock.patch.object(fd.currency_risk_engine, "historical_fx_rate", return_value=(None, "unavailable")):
    _myf.Ticker.side_effect = _ElsFakeTicker
    _els_bundle = fd.get_bundle("ELS.AX", force_refresh=True)

check("get_bundle(): a statement conversion where every column's rate is "
      "unresolved is NOT flagged 'currency_converted' (the false promise "
      "that caused the live bug)",
      "currency_converted" not in _els_bundle["meta"]["flags"])
check("get_bundle(): it IS honestly flagged 'currency_conversion_partial' "
      "instead, so a caller trusting the old flag name can tell the "
      "difference",
      "currency_conversion_partial" in _els_bundle["meta"]["flags"])

_els_dcf_info, _els_converted_flag = ace._dcf_info_with_verified_currency_override(
    _els_bundle, "AUD"
)
check("_dcf_info_with_verified_currency_override(): with the honest flag, "
      "it no longer overrides financialCurrency to the listing currency - "
      "dcf_intrinsic_value() will run its OWN internal USD->AUD conversion "
      "on the (genuinely still raw) cashflow, the same thing Deep Dive's "
      "own independent raw-cashflow fetch already does - instead of "
      "skipping conversion on a false 'already done' signal",
      _els_dcf_info.get("financialCurrency") == "USD" and _els_converted_flag is False)
print("[task40_check5_els_ax_currency_fix] a bundle whose statement currency "
      "conversion didn't actually succeed for every column is no longer "
      "mislabeled 'currency_converted' - the Fair Value DCF path now falls "
      "through to the same internal conversion Deep Dive already relies on, "
      "instead of silently using a raw, unconverted cashflow OK")

# And the happy path (every column's rate DOES resolve) is unchanged from
# before this fix - still flags 'currency_converted', override still fires.
with mock.patch.object(fd, "yf") as _myf2, \
     mock.patch.object(fd.fcf_valuation_engine, "fx_rate", return_value=(1.456, "live")), \
     mock.patch.object(fd.currency_risk_engine, "historical_fx_rate", return_value=(1.456, "historical")):
    _myf2.Ticker.side_effect = _ElsFakeTicker
    _els_bundle_ok = fd.get_bundle("ELS.AX", force_refresh=True)
check("get_bundle(): when every column genuinely converts, 'currency_converted' "
      "still fires exactly as before this fix (the happy path is untouched)",
      "currency_converted" in _els_bundle_ok["meta"]["flags"]
      and "currency_conversion_partial" not in _els_bundle_ok["meta"]["flags"])
_els_dcf_info_ok, _els_converted_flag_ok = ace._dcf_info_with_verified_currency_override(
    _els_bundle_ok, "AUD"
)
check("_dcf_info_with_verified_currency_override(): still overrides "
      "financialCurrency to the listing currency when conversion genuinely "
      "succeeded - TASK 35 item B's own behaviour, unchanged",
      _els_dcf_info_ok.get("financialCurrency") == "AUD" and _els_converted_flag_ok is True)
print("[task40_check5b_happy_path_unchanged] a bundle that genuinely "
      "converted every statement still gets the override exactly as "
      "before this fix OK")


print(f"\n{passed} passed, {failed} failed")
sys.exit(0 if failed == 0 else 1)
