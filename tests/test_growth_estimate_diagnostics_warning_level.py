"""
Growth-estimate diagnostics: WARNING level + raw-frame/null-row dumps
(owner-directed, urgent, 30 Sep 2026).

Root cause: a Dow 30 rescan logged "growth source - Yahoo 5y 0" identical
to the pre-fix (29 Sep) behaviour - LTG is not being read in production -
but the two existing one-per-process diagnostics (index labels, raw LTG
value) never appeared in the logs at all. Both called _growth_logger.
info(); only server.py calls logging.basicConfig(level=logging.INFO), so
the Streamlit subprocess (where "Rescan now" actually runs) has no INFO
handler and silently drops every one of these records.

This file covers:
  - both pre-existing diagnostics now log at WARNING, not INFO
  - a new one-per-process WARNING dumps the full raw growth_estimates
    frame (repr/dtypes/columns/index) plus the parse outcome (labels
    matched, scale, value, status) for the first ticker each process
  - a new one-per-process WARNING fires when a matched label (LTG/+5y)
    has no usable value across every candidate column - this is the
    diagnostic that would show a wrong/missing column name, which the
    raw-value diagnostic alone can never surface (it only fires when a
    value WAS found)
  - the underlying parse behaviour (value/status returned) is completely
    unchanged by any of this - these are pure additive diagnostics

Run: python3 tests/test_growth_estimate_diagnostics_warning_level.py
"""
import logging
import os
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

import capm_engine as ce


class _CapturingHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append((record.levelname, record.getMessage()))


def _reset_once_flags():
    ce._growth_estimate_labels_logged = False
    ce._growth_estimate_raw_value_logged = False
    ce._growth_estimate_raw_frame_logged = False
    ce._growth_estimate_null_row_logged = False


handler = _CapturingHandler()
ce._growth_logger.addHandler(handler)
ce._growth_logger.setLevel(logging.WARNING)


# ======================================================================
# CHECK 1: a clean LTG match (correct columns) - all three diagnostics
# fire, all at WARNING, and the returned value/status are unchanged from
# before this fix (0.103, "ok").
# ======================================================================
_reset_once_flags()
handler.records.clear()
df_ok = pd.DataFrame({"stock": [0.0, 0.01, 0.02, 10.3]}, index=["0q", "0y", "+1y", "LTG"])
with mock.patch("nightly_scan._yf_call_with_retry", return_value=df_ok):
    val, status = ce._fetch_growth_estimates_5y_uncached("AAPL")

assert abs(val - 0.103) < 1e-9, val
assert status == "ok", status
levels = {lvl for lvl, _ in handler.records}
assert levels == {"WARNING"}, f"expected only WARNING records, got {levels}"
messages = [msg for _, msg in handler.records]
assert any("index labels" in m for m in messages), messages
assert any("raw value for label" in m for m in messages), messages
assert any("RAW FRAME DUMP" in m for m in messages), messages
assert not any("found no usable value" in m for m in messages), "no null-row case here"
print("[clean_match_warning_level] index-labels, raw-value and frame-dump diagnostics "
      "all fire at WARNING (not INFO); parse result unchanged (0.103, 'ok') OK")


# ======================================================================
# CHECK 2: a matched label with NO usable value across every candidate
# column (the "wrong column name" hypothesis) - the new null-row
# diagnostic fires, the frame dump still fires, and the function still
# correctly falls through to (None, "no_coverage") exactly as before.
# ======================================================================
_reset_once_flags()
handler.records.clear()
df_bad_cols = pd.DataFrame({"totallyDifferentColumn": [0.0, 0.01, 0.02, 10.3]},
                            index=["0q", "0y", "+1y", "LTG"])
with mock.patch("nightly_scan._yf_call_with_retry", return_value=df_bad_cols):
    val2, status2 = ce._fetch_growth_estimates_5y_uncached("MSFT")

assert val2 is None and status2 == "no_coverage", (val2, status2)
messages2 = [msg for _, msg in handler.records]
assert any("found no usable value" in m and "LTG" in m for m in messages2), messages2
assert any("RAW FRAME DUMP" in m and "MSFT" in m for m in messages2), messages2
assert not any("raw value for label" in m for m in messages2), (
    "the raw-value diagnostic must NOT fire when no value was ever found"
)
print("[null_row_diagnostic] a matched label with no usable value logs the null-row "
      "WARNING (naming the label and the candidate columns tried) and still falls "
      "through to (None, 'no_coverage') unchanged OK")


# ======================================================================
# CHECK 3: each of the four diagnostics fires at most ONCE per process,
# even across multiple tickers.
# ======================================================================
_reset_once_flags()
handler.records.clear()
with mock.patch("nightly_scan._yf_call_with_retry", return_value=df_ok):
    ce._fetch_growth_estimates_5y_uncached("AAA")
    ce._fetch_growth_estimates_5y_uncached("BBB")
    ce._fetch_growth_estimates_5y_uncached("CCC")

label_count = sum(1 for _, m in handler.records if "index labels" in m)
raw_value_count = sum(1 for _, m in handler.records if "raw value for label" in m)
frame_dump_count = sum(1 for _, m in handler.records if "RAW FRAME DUMP" in m)
assert label_count == 1, label_count
assert raw_value_count == 1, raw_value_count
assert frame_dump_count == 1, frame_dump_count
print("[once_per_process] each diagnostic fires exactly once across three calls in "
      "the same process OK")


# ======================================================================
# CHECK 4: empty df still gets a frame-dump WARNING (the "no data at
# all" case is itself diagnostic information), and still returns
# (None, "no_coverage") unchanged.
# ======================================================================
_reset_once_flags()
handler.records.clear()
df_empty = pd.DataFrame()
with mock.patch("nightly_scan._yf_call_with_retry", return_value=df_empty):
    val3, status3 = ce._fetch_growth_estimates_5y_uncached("EMPTYCO")
assert val3 is None and status3 == "no_coverage", (val3, status3)
assert any("RAW FRAME DUMP" in m and "EMPTYCO" in m for _, m in handler.records), handler.records
print("[empty_df_still_dumped] an empty growth_estimates frame still triggers the "
      "one-per-process frame-dump WARNING, and still returns (None, 'no_coverage') OK")


ce._growth_logger.removeHandler(handler)
print("\nALL GROWTH-ESTIMATE DIAGNOSTICS (WARNING-LEVEL) TESTS PASSED")
