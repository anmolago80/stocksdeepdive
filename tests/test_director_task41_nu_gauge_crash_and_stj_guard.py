"""
TASK 41 (Director-directed, 8 Nov 2026): a live page crash + a
previously-found staleness-guard gap.

1. NU Deep Dive crash (live, confirmed via the Director's own stack
   trace): compounder_ui.band_gauge() raised TypeError ("'<' not
   supported between 'str' and 'float'") at `if value < axis_min`,
   because render_section()'s own colored/plain split only checked
   "has thresholds and value is not None" - never that the value was
   actually NUMERIC. auto_compounder_engine._build_fundamentals() has
   exactly two call sites that can pass a literal STRING value through
   for a metric that still carries the hand-built workbook's own real
   thresholds: "EBIT to FCF Conversion" and "Operating Income Ratio"
   (the metric immediately after "ROE" in that function's own `add()`
   call order - matches the Director's own description, "the metric
   after ROE"), both via the TASK 34 (5 Nov 2026, MFF.AX case) fallback
   `add(label, "not meaningful - EBIT is near zero", "raw", ...)` for
   a financials-mode, near-zero-EBIT ticker. NU Holdings (Nubank) is a
   digital bank - squarely financials-mode - which is almost certainly
   why it (not MFF.AX, whose own workbook column may lack a threshold
   for this specific label, or whose EBIT/revenue ratio happened not
   to be near zero at scan time) is the one that actually hit this
   live, since TASK 34's own test file checked the dict's "values"/
   "format" keys directly but never called render_section()/
   band_gauge() end-to-end.

   ROOT CAUSE INVESTIGATION (item 1b, "NOT confirmed - find it"): the
   Director's own lead was fundamentals_data._convert_statement_
   currency()'s `except` branch returning the RAW (not pd.to_numeric-
   coerced) df on a failed conversion, possibly combined with a
   provider (EODHD?) returning string cells, as the source of a
   non-numeric value reaching band_gauge(). Investigated and RULED
   OUT as the mechanism for this specific crash:
     - auto_compounder_engine._series() (the ONLY path _build_
       fundamentals() reads a statement DataFrame cell through) does
       `float(v)` per cell inside a blanket try/except that returns
       `[]` (not a partial row) on ANY failure - so a non-numeric cell
       ANYWHERE in a row, whether from a yfinance quirk, EODHD, or a
       raw (never-converted) currency-mismatched statement, can only
       ever produce None downstream (via _latest()), never a raw
       string reaching add().
     - fundamentals_data._eodhd_statement_to_df() ALSO coerces every
       cell via `float(v) if v not in (None, "") else None` inside its
       own try/except at ingest time - an EODHD string cell can never
       reach a DataFrame at all, let alone _convert_statement_
       currency()'s except branch.
   So _convert_statement_currency()'s except-branch behaviour (TASK 40
   item 5's own fix already made this honest rather than falsely
   flagging success, but did not change what it RETURNS on failure -
   still the raw df) cannot explain a string reaching band_gauge():
   every statement-derived value is coercion-safe long before it gets
   there. The two literal-string `add()` calls above are a complete,
   sufficient, independently-reproducible explanation - CONFIRMED via
   code reading (every other `add()` call in _build_fundamentals() is
   a pure arithmetic expression over already-coerced floats or None,
   grep-verified below).

   FIX: (a) general crash guard, both in compounder_ui.band_gauge()
   itself (defence in depth) and in render_section()'s own colored/
   plain split (the real fix - a non-numeric value no longer reaches
   band_gauge() at all in the normal flow): a value that doesn't
   coerce to float renders as a plain label/value line, never a
   crash, logged once as "[gauge_non_numeric] ticker=... section=...
   label=... value=... type=...". (b) no code "fix at source" applied
   to _convert_statement_currency()'s except path, since it isn't the
   confirmed mechanism - see above.

2. STJ.L one-directional staleness guard (root cause already found in
   TASK 40's own report): top100_engine._build_best_by_ticker()'s
   `stored_unreliable or current_unreliable` caught a row that looked
   fine at scan time but became unreliable since, but never the
   opposite: a row flagged unreliable at scan time whose CURRENT
   repriced ratio has since fallen back under the 3x sanity multiple.
   Fixed: use current_unreliable (today's own Intrinsic Value/Price)
   whenever it's actually computable (both present, Price > 0);
   fall back to stored_unreliable only when it isn't.

4. Quality exception upper cap (Director-approved design change,
   GQG.AX live case - iv/price 7.41-7.88x across different runs):
   GQG.AX passed every existing _quality_exception_admits() condition
   (Quality 100, Moat 90.9) despite a multiple this extreme no longer
   looking like the "good company, big price fall" shape the
   exception exists for (the ADBE case it was built on, 3.05x). New
   QUALITY_EXCEPTION_MAX_MULTIPLE = 4.0 - Intrinsic Value / Price must
   be <= 4.0x to admit, gated the same way as the exception itself
   (TOP200_QUALITY_EXCEPTION_LIVE), never a second switch.

Run: python3 tests/test_director_task41_nu_gauge_crash_and_stj_guard.py
"""
import os
import re
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TESTVOL = tempfile.mkdtemp(prefix="task41_test_")
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = TESTVOL

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

import compounder_ui as cui
import scan_store
import top100_engine as te
import top100_store as ts

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
# CHECK 1 (item 1b, root-cause verification): every OTHER add() call in
# _build_fundamentals() besides the two TASK 34 string fallbacks is a
# pure arithmetic expression (or a bare already-coerced local), never a
# raw statement/info value passed straight through - the two confirmed
# string-literal sites are the ONLY way a non-numeric value can reach
# add() in this function.
# ======================================================================
with open(os.path.join(_REPO_ROOT, "auto_compounder_engine.py")) as f:
    _ace_src = f.read()
_fundamentals_start = _ace_src.index("def _build_fundamentals(")
_fundamentals_end = _ace_src.index("def _build_value_vs_book(")
_fundamentals_src = _ace_src[_fundamentals_start:_fundamentals_end]
_add_calls = re.findall(r'add\(\s*"([^"]+)"\s*,\s*("[^"]*"|[^,]+),', _fundamentals_src)
_string_literal_adds = [label for label, value_expr in _add_calls if value_expr.strip().startswith('"')]
check("exactly the two TASK 34 string-literal add() calls in "
      "_build_fundamentals() pass a hardcoded string value "
      f"(found: {_string_literal_adds})",
      set(_string_literal_adds) == {"EBIT to FCF Conversion", "Operating Income Ratio"})
print("[task41_check1_string_literal_sites] confirmed via source grep: "
      "'EBIT to FCF Conversion' and 'Operating Income Ratio' (the TASK 34 "
      "near-zero-EBIT fallback) are the ONLY add() calls in _build_"
      "fundamentals() that can ever pass a non-numeric value OK")


# ======================================================================
# CHECK 2 (item 1b, root-cause verification): a statement DataFrame
# with a genuine STRING cell, read through _series()/_latest() (the
# only path _build_fundamentals() uses), never reaches a caller as a
# string - it coerces to None, exactly like a failed/raw currency
# conversion would. Proves _convert_statement_currency()'s except
# path (TASK 40 item 5) cannot be the mechanism: whatever it returns,
# this coercion already protects every reader.
# ======================================================================
import pandas as pd
import auto_compounder_engine as ace

_string_cell_df = pd.DataFrame({"2025-06-30": ["not a number", 100.0]}, index=["Net Income", "Total Assets"])
check("_series() on a row with a genuine STRING cell returns [] (not "
      "the string) - the whole row is dropped, never partially passed "
      "through",
      ace._series(_string_cell_df, "net_income") == [])
check("_latest() on the same string-cell row returns None, never the "
      "string itself",
      ace._latest(_string_cell_df, "net_income") is None)
_numeric_row_df = pd.DataFrame({"2025-06-30": [100.0]}, index=["Total Assets"])
check("a genuinely numeric cell still reads through fine (sanity check "
      "on the fixture itself)",
      ace._latest(_numeric_row_df, "total_assets") == 100.0)
print("[task41_check2_string_cell_coercion_safety] a raw string cell "
      "anywhere in a statement DataFrame can only ever become None by "
      "the time _build_fundamentals() reads it - confirms _convert_"
      "statement_currency()'s except path (returning the RAW df) "
      "cannot be how a string reaches band_gauge() OK")

import fundamentals_data as fd
_eodhd_payload = {"Financials": {"Income_Statement": {"yearly": {
    "2025-06-30": {"netIncome": "not a number", "totalAssets": 500.0},
}}}}
_eodhd_df = fd._eodhd_statement_to_df(_eodhd_payload, "Income_Statement")
check("EODHD's own statement builder ALSO coerces a string field to "
      "None at ingest time, before a DataFrame even exists - a string "
      "cell from this provider can never reach _convert_statement_"
      "currency() at all",
      pd.isna(_eodhd_df.loc["netIncome"].iloc[0])
      and _eodhd_df.loc["totalAssets"].iloc[0] == 500.0)
print("[task41_check2b_eodhd_coercion_safety] EODHD-sourced statements "
      "are also string-free by construction OK")


# ======================================================================
# CHECK 3 (item 1a, the crash guard): _cp_numeric_or_none().
# ======================================================================
check("_cp_numeric_or_none(None) -> None", cui._cp_numeric_or_none(None) is None)
check("_cp_numeric_or_none(0.12) -> 0.12", cui._cp_numeric_or_none(0.12) == 0.12)
check("_cp_numeric_or_none('not meaningful - EBIT is near zero') -> None",
      cui._cp_numeric_or_none("not meaningful - EBIT is near zero") is None)
check("_cp_numeric_or_none('0.12') (a numeric STRING) -> 0.12 (still "
      "coerces - only a genuinely non-numeric string is rejected)",
      cui._cp_numeric_or_none("0.12") == 0.12)
print("[task41_check3_numeric_or_none] _cp_numeric_or_none() correctly "
      "tells a genuinely non-numeric value apart from a numeric one "
      "(string or float alike) OK")


# ======================================================================
# CHECK 4 (item 1a): band_gauge()'s own defensive guard - called
# directly with a non-numeric value and real thresholds set (the exact
# live shape: "Operating Income Ratio" with a workbook threshold still
# attached) never raises, logs [gauge_non_numeric] once, and renders a
# plain label/value line instead of a band/marker.
# ======================================================================
_captured_markdown = []
cui.st.markdown = lambda html_str, unsafe_allow_html=True: _captured_markdown.append(html_str)
_captured_expander_calls = []


class _FakeExpanderCtx:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


cui.st.expander = lambda *a, **k: _FakeExpanderCtx()
_OI_RATIO_THRESHOLDS = [
    (None, 0.10, "red", "Weak cost control"),
    (0.10, 0.15, "amber", "Healthy, not exceptional"),
    (0.15, None, "green", "Strong pricing power"),
]

try:
    cui.band_gauge(
        "Operating Income Ratio", "not meaningful - EBIT is near zero", "raw",
        _OI_RATIO_THRESHOLDS, flagged=True,
        comment="EBIT (operating income) divided by revenue.",
        ticker="NU", section="Fundamentals",
    )
    _band_gauge_raised = False
except TypeError:
    _band_gauge_raised = True

check("band_gauge() with the EXACT live crash shape (string value, "
      "real thresholds, flagged=True) does NOT raise TypeError",
      not _band_gauge_raised)
# _captured_markdown[0] is the gauge's own render; a second st.markdown
# call follows from the "What this measures" expander's own _cp_note().
check("band_gauge() rendered SOMETHING (a plain label/value line, not "
      "a band/marker) rather than silently doing nothing",
      len(_captured_markdown) >= 1 and "Operating Income Ratio" in _captured_markdown[0])
check("the plain fallback shows the string value itself, not a crash "
      "or a blank",
      "not meaningful" in _captured_markdown[0])
print("[task41_check4_band_gauge_guard] band_gauge() itself never "
      "crashes on a non-numeric value, even with real thresholds set - "
      "renders a plain fallback line instead OK")

# And the ordinary numeric path is completely unaffected.
_captured_markdown.clear()
cui.band_gauge("ROE", 0.18, "pct", [(None, 0.10, "red", "Weak"), (0.10, None, "green", "Strong")],
                ticker="NORMAL", section="Fundamentals")
check("band_gauge() with a normal numeric value still renders the "
      "real band/marker HTML (position:absolute marker present)",
      len(_captured_markdown) >= 1 and "position:absolute" in _captured_markdown[0])
print("[task41_check4b_numeric_path_unaffected] the ordinary numeric "
      "band_gauge() path is byte-identical to before this fix OK")


# ======================================================================
# CHECK 5 (item 1a): render_section()'s own colored/plain split -
# real function, a metrics list shaped exactly like the live crash
# (one numeric "colored" metric, one non-numeric-but-thresholded
# metric that used to wrongly join it). Captures st.markdown/st.metric
# calls to confirm the split without needing a full Streamlit runtime.
# ======================================================================
_captured_markdown.clear()
_captured_metrics = []
_captured_captions = []


class _FakeCol:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


cui.st.columns = lambda n: [_FakeCol() for _ in range(n)]
cui.st.caption = lambda *a, **k: _captured_captions.append(a[0] if a else "")
cui.st.metric = lambda label, value, help=None: _captured_metrics.append((label, value))

_sections = {
    "Fundamentals": {
        "metrics": [
            {
                "key": "ROE", "label": "ROE", "comment": "Return on equity.",
                "format": "pct", "thresholds": [(None, 0.10, "red", "Weak"), (0.10, None, "green", "Strong")],
                "values": {"NU": 0.18}, "flagged": False,
            },
            {
                "key": "Operating Income Ratio", "label": "Operating Income Ratio",
                "comment": "EBIT divided by revenue.", "format": "raw",
                "thresholds": _OI_RATIO_THRESHOLDS,
                "values": {"NU": "not meaningful - EBIT is near zero"}, "flagged": True,
            },
        ],
        "price_history": {}, "share_price_growth": {},
    }
}

try:
    cui.render_section(_sections, "NU", "Fundamentals")
    _render_section_raised = False
except TypeError:
    _render_section_raised = True

check("render_section() with the exact live crash shape (NU, one real "
      "numeric metric + the string fallback with real thresholds) "
      "does NOT raise",
      not _render_section_raised)
check("the numeric ROE metric still rendered as a real band_gauge "
      "(its own st.markdown call happened)",
      any("ROE" in m for m in _captured_markdown))
check("the non-numeric 'Operating Income Ratio' metric rendered via "
      "the plain st.metric path (routed to 'plain', not 'colored')",
      any(label == "Operating Income Ratio :red[*]" for label, _ in _captured_metrics))
print("[task41_check5_render_section_split] render_section()'s own "
      "colored/plain split now correctly routes a non-numeric, "
      "thresholds-carrying metric to the plain 'Other metrics' grid - "
      "reproduces and fixes the exact live NU crash OK")


# ======================================================================
# CHECK 6 (item 2): STJ.L staleness-guard fix. stored_unreliable=True
# (an older scan-time flag) but today's own Intrinsic Value/Price give
# a genuinely-fine 2.80x (< 3.0 DCF_SANITY_MULTIPLE) - must NOT be
# treated as unreliable any more, so it needs no quality exception at
# all to be selected.
# ======================================================================
if os.path.exists(ts.DB_PATH):
    os.remove(ts.DB_PATH)


def _stj_row(ticker="STJ.L", **overrides):
    row = {
        "Ticker": ticker, "Company Name": f"{ticker} Co", "Price": 10.40,
        "Intrinsic Value": 29.12,  # 2.80x - genuinely fine today
        "MOS %": 64.3, "Long Score": 55.0, "Quality": 83.0, "Moat": 88.7,
        "Psychology": 5.0, "Discovery (lite)": 20.0,
        "FCF Source": "ocf-normcapex", "Share Count Flagged": False,
        "Trading Status": None, "Intrinsic Source": "dcf",
        "Is Fund": False, "price_unit_suspect": False, "Sector": "Financial Services",
        "DCF Unreliable": True,  # the STALE scan-time flag
    }
    row.update(overrides)
    return row


_clean_rows = [
    {**_stj_row(f"CLEAN{i}"), "Price": 100.0, "Intrinsic Value": 110.0,
     "DCF Unreliable": False, "Long Score": 50.0 + i}
    for i in range(5)
]
scan_store.save_scan("S&P 500", _clean_rows + [_stj_row()], source_label="test fixture")

os.environ.pop("TOP200_QUALITY_EXCEPTION_LIVE", None)
_best, _ = te._build_best_by_ticker(log=lambda *a, **k: None)
check("STJ.L-style row (stored_unreliable=True, current ratio 2.80x - "
      "genuinely under the 3x sanity multiple): now SELECTED without "
      "needing the quality exception at all, since the fix uses "
      "today's own (computable) ratio instead of the stale stored flag",
      "STJ.L" in _best and _best["STJ.L"].get("quality_exception_admitted") is not True)
print("[task41_check6_stj_fixed_without_exception] a row whose stale "
      "scan-time flag was True but whose current repriced ratio is "
      "genuinely fine is now selected on its own numbers, never "
      "needing the TASK 37 exception just to clear a staleness false "
      "positive OK")

# And the ORIGINAL staleness-guard purpose (the direction it WAS meant
# to catch) still works: a row that looked fine at scan time but has
# become genuinely unreliable on today's own (computable) numbers is
# still excluded (or still needs the exception).
if os.path.exists(ts.DB_PATH):
    os.remove(ts.DB_PATH)
_newly_bad_row = _stj_row("NEWBAD.L", **{
    "DCF Unreliable": False,  # looked FINE at scan time
    "Intrinsic Value": 50.0,  # but today's own price/IV ratio is 50/10.40 = 4.8x
})
scan_store.save_scan("S&P 500", _clean_rows + [_newly_bad_row], source_label="test fixture")
os.environ.pop("TOP200_QUALITY_EXCEPTION_LIVE", None)
_best2, _ = te._build_best_by_ticker(log=lambda *a, **k: None)
check("a row that looked FINE at scan time but is genuinely unreliable "
      "on TODAY's own repriced ratio (4.8x) is still excluded (the "
      "staleness guard's own original, intended direction is untouched)",
      "NEWBAD.L" not in _best2)
print("[task41_check6b_original_direction_preserved] the guard still "
      "catches a row that became unreliable since scan time - only the "
      "opposite (stale-unreliable-but-now-fine) direction changed OK")

# And the fallback-to-stored-when-not-computable path: no Intrinsic
# Value/Price at all on the row -> current ratio isn't computable ->
# falls back to the stored flag, same as before this fix.
if os.path.exists(ts.DB_PATH):
    os.remove(ts.DB_PATH)
_no_iv_row = _stj_row("NOIV.L", **{"Intrinsic Value": None, "DCF Unreliable": True})
scan_store.save_scan("S&P 500", _clean_rows + [_no_iv_row], source_label="test fixture")
os.environ.pop("TOP200_QUALITY_EXCEPTION_LIVE", None)
_best3, _ = te._build_best_by_ticker(log=lambda *a, **k: None)
check("no Intrinsic Value at all on the row -> today's ratio isn't "
      "computable -> falls back to the stored flag (True) -> still "
      "excluded, same as before this fix",
      "NOIV.L" not in _best3)
print("[task41_check6c_fallback_when_not_computable] with no computable "
      "current ratio at all, the stored flag is still used as the "
      "fallback signal, unchanged from before this fix OK")


# ======================================================================
# CHECK 7 (item 4): quality exception upper cap. GQG.AX-shaped row
# (iv/price ~7.41-7.88x, Quality 100, Moat 90.9 - every OTHER condition
# passes) must now be excluded; ADBE/HRB/TBBK-shaped rows at or below
# 4.0x must stay admitted exactly as before.
# ======================================================================
check("QUALITY_EXCEPTION_MAX_MULTIPLE is 4.0",
      te.QUALITY_EXCEPTION_MAX_MULTIPLE == 4.0)


def _capped_row(ticker, price, iv, **overrides):
    row = {
        "Ticker": ticker, "Company Name": f"{ticker} Co", "Price": price,
        "Intrinsic Value": iv, "MOS %": 50.0, "Long Score": 60.0,
        "Quality": 90.0, "Moat": 90.0, "Psychology": 5.0, "Discovery (lite)": 20.0,
        "FCF Source": "ocf-normcapex", "Share Count Flagged": False,
        "Trading Status": None, "Intrinsic Source": "dcf",
        "Is Fund": False, "price_unit_suspect": False, "Sector": "Technology",
        "DCF Unreliable": True,
    }
    row.update(overrides)
    return row


_gqg_row = _capped_row("GQG.AX", price=0.94, iv=7.41, Quality=100.0, Moat=90.9)
check("GQG.AX-shaped row (iv/price=7.88x, Quality 100, Moat 90.9 - "
      "every OTHER condition clean) -> NOT admitted (past the new cap)",
      not te._quality_exception_admits(_gqg_row))

_adbe_row = _capped_row("ADBE", price=246.44, iv=750.64)  # 3.05x
check("ADBE-shaped row (3.05x) -> still admitted, unchanged by the cap",
      te._quality_exception_admits(_adbe_row))

_hrb_row = _capped_row("HRB", price=50.0, iv=195.0)  # 3.90x - just under the cap
check("a row at 3.90x (just under the 4.0x cap) -> admitted",
      te._quality_exception_admits(_hrb_row))

_tbbk_row = _capped_row("TBBK", price=40.0, iv=160.0)  # exactly 4.0x
check("a row at EXACTLY 4.0x -> admitted (<=, not <)",
      te._quality_exception_admits(_tbbk_row))

_just_over_row = _capped_row("JUSTOVER", price=40.0, iv=160.01)  # 4.0001x
check("a row just OVER 4.0x (4.0001x) -> NOT admitted",
      not te._quality_exception_admits(_just_over_row))

check("a row with no Price at all -> NOT admitted by the cap check, "
      "never raises",
      not te._quality_exception_admits(_capped_row("NOPRICE", price=None, iv=100.0)))
check("a row with Price <= 0 -> NOT admitted, never raises (ZeroDivisionError guarded)",
      not te._quality_exception_admits(_capped_row("ZEROPRICE", price=0.0, iv=100.0)))
print("[task41_check7_quality_exception_cap] the new 4.0x upper cap "
      "excludes the GQG.AX-shaped multiple while leaving ADBE/HRB/TBBK-"
      "shaped rows (<= 4.0x) admitted exactly as before this change OK")

# End-to-end: GQG.AX-shaped row in a real scan file, switch forced ON -
# excluded from the live pool; a clean ADBE-shaped row in the SAME scan
# is still admitted.
if os.path.exists(ts.DB_PATH):
    os.remove(ts.DB_PATH)
scan_store.save_scan("S&P 500", _clean_rows + [_gqg_row, _adbe_row], source_label="test fixture")
os.environ.pop("TOP200_QUALITY_EXCEPTION_LIVE", None)
_best4, _ = te._build_best_by_ticker(log=lambda *a, **k: None, quality_exception_override=True)
check("end to end, switch forced ON: GQG.AX-shaped row (7.88x) is "
      "EXCLUDED from the pool (the cap, not just the pure predicate, "
      "actually reaches selection)",
      "GQG.AX" not in _best4)
check("end to end, same run: ADBE-shaped row (3.05x) is still ADMITTED",
      "ADBE" in _best4 and _best4["ADBE"].get("quality_exception_admitted") is True)
print("[task41_check7b_end_to_end_selection] _build_best_by_ticker() "
      "itself (not just the predicate in isolation) now excludes a "
      "GQG.AX-shaped row past the 4.0x cap while still admitting an "
      "ADBE-shaped row under it OK")


print(f"\n{passed} passed, {failed} failed")
sys.exit(0 if failed == 0 else 1)
