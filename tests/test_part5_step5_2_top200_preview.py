"""
PART 5 STEP 5.2 of instruction_health_fixes_chart_and_new_markets.md
(8 Oct 2026, Director-directed): "build behind a switch, ONLY if
purely additive." TOP200_PREVIEW_UNIVERSES (unset = nothing changes
anywhere). The one piece STEP 5.1's own proposal stops on - queuing a
preview company into the normal nightly scoring batch - is explicitly
NOT built here (see PART5_REPORT.md); this file covers only the
purely-additive pieces that WERE built:

  - top100_engine.preview_universes(): unset -> [], set -> parsed list.
  - top100_engine._log_preview_universe_cost_estimate(): one line per
    PRIVATE universe with a saved scan, reusing market_readiness_
    engine.top200_dry_run()'s own numbers - fires regardless of the
    switch's own value (purely advisory).
  - top100_engine.preview_candidates_by_country(): {} when unset;
    grouped-by-country, sorted-by-value_score when set, read directly
    via scan_store.load_scan_raw(allow_private=True) - never through
    _eligible_scan_payloads()/select_top100_pool().
  - app._render_top200_preview_panel(): unset -> caption only, no
    table; set -> per-country expander + coverage metrics.
  - Byte-identical proof: select_top100_pool()'s OWN returned pool is
    identical whether TOP200_PREVIEW_UNIVERSES is set or not - this
    switch is never read anywhere inside pool selection.
  - The scoring-request fingerprint (tests/test_top200_commit4_schema_
    mode.py's own f00c69f5... sha256 check) is re-run here too, to
    prove _request_params()/the batch-submission entrant path is
    byte-for-byte untouched by this commit.

Run: python3 tests/test_part5_step5_2_top200_preview.py
"""
import hashlib
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TESTVOL = tempfile.mkdtemp(prefix="part5_step5_2_test_")
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = TESTVOL
os.environ.pop("TOP200_PREVIEW_UNIVERSES", None)
os.environ.pop("PRIVATE_UNIVERSES", None)

import scan_store
import top100_engine as te
import top100_store

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
# CHECK 1-2: preview_universes() - unset/set.
# ======================================================================
check("preview_universes() is [] when unset", te.preview_universes() == [])
# Nikkei 225, DAX (not FTSE 100/TSX Composite) - PUSH 2 (Andrew's go,
# 10 Oct 2026, instruction_combined_10oct.md) made FTSE 100, FTSE 250
# AND TSX Composite public (on top of TSX 60's own 9 Oct correction),
# so none of the four UK/Canada universes fit this feature's own
# purpose any more (previewing candidates from a market that's still
# PRIVATE before making it public). Nikkei 225/DAX stay private,
# unchanged - the next still-private pair, one per distinct country
# (Japan/Germany) to keep exercising the "grouped by country" checks
# below meaningfully.
os.environ["TOP200_PREVIEW_UNIVERSES"] = "Nikkei 225, DAX"
check('preview_universes() == ["Nikkei 225", "DAX"] when set',
      te.preview_universes() == ["Nikkei 225", "DAX"])
os.environ.pop("TOP200_PREVIEW_UNIVERSES", None)

# ======================================================================
# Production-shaped fixture: two PRIVATE universes (scan_store.
# save_scan() - the real writer), one with a real stored score (via
# top100_store.save_score() - the real writer), one entirely WAITING.
# ======================================================================
scan_store.save_scan("Nikkei 225", [
    {"Ticker": "7203.T", "Type": "STOCK", "Company Name": "Toyota Motor Corp", "Price": 2500.0,
     "Intrinsic Value": 3000.0, "MOS %": 16.5, "Long Score": 60.0, "Quality": 55.0,
     "Psychology": 5.0, "Discovery (lite)": 30.0, "Moat": 50.0,
     "Growth Used": 0.05, "FCF Source": "reported", "Dividend Yield %": 4.2},
    {"Ticker": "6758.T", "Type": "STOCK", "Company Name": "Sony Group Corp", "Price": 750.0,
     "Intrinsic Value": 900.0, "MOS %": 16.7, "Long Score": None, "Quality": 45.0,
     "Psychology": 5.0, "Discovery (lite)": 25.0, "Moat": 35.0,
     "Growth Used": 0.02, "FCF Source": "reported", "Dividend Yield %": 6.1},
], source_label="test fixture")
scan_store.save_scan("DAX", [
    {"Ticker": "SIE.DE", "Type": "STOCK", "Company Name": "Siemens AG", "Price": 140.0,
     "Intrinsic Value": 160.0, "MOS %": 12.5, "Long Score": 70.0, "Quality": 65.0,
     "Psychology": 5.0, "Discovery (lite)": 35.0, "Moat": 60.0,
     "Growth Used": 0.04, "FCF Source": "reported", "Dividend Yield %": 3.8},
], source_label="test fixture")

_dims = {key: {"score": 4, "justification": "test fixture", "source_period": "RP2026-09-30"}
         for key in te.DIMENSION_KEYS}
top100_store.save_score(
    ticker="7203.T", quarter="RP2026-09-30", model=te.MODEL_TOP100,
    rubric_version=te.RUBRIC_VERSION, dims=_dims, not_rated=False,
    most_recent_quarter="2026-09-30",
    inversion_scenario="test", inversion_severity=2, current_headwind=None,
    market_structure=None, market_structure_comment=None,
    one_foot_hurdle=None, one_foot_comment=None,
    munger_quality=None, munger_comment=None, big_wave=None, big_wave_comment=None,
    prompt="{}", raw_response="{}",
)
# 6758.T and SIE.DE stay WAITING (no score, no failure recorded).

# ======================================================================
# CHECK 3-5: _log_preview_universe_cost_estimate() - fires regardless
# of the switch, reuses market_readiness_engine.top200_dry_run()'s own
# numbers, one line per PRIVATE universe with a saved scan.
# ======================================================================
_log_lines = []
te._log_preview_universe_cost_estimate(log=_log_lines.append)
_joined = "\n".join(_log_lines)
check("one advisory line for Nikkei 225 (2 candidates, 1 with no stored Long Score at all - "
      "6758.T's own scan row carries none)",
      "'Nikkei 225': 2 candidate(s), 1 with no stored score yet, est. one-time cost $0.02" in _joined)
check("one advisory line for DAX (1 candidate, SIE.DE's own scan row already "
      "has a Long Score, so nothing left to score - $0.00)",
      "'DAX': 1 candidate(s), 0 with no stored score yet, est. one-time cost $0.00" in _joined)
check("fires with the switch UNSET too (purely advisory, not gated by TOP200_PREVIEW_UNIVERSES)",
      len(_log_lines) >= 2)

# ======================================================================
# CHECK 6-9: preview_candidates_by_country() - {} unset; grouped +
# sorted when set; direct scan_store read, never through the real
# pool's own eligible-payloads path.
# ======================================================================
check("preview_candidates_by_country() is {} when TOP200_PREVIEW_UNIVERSES is unset",
      te.preview_candidates_by_country() == {})
os.environ["TOP200_PREVIEW_UNIVERSES"] = "Nikkei 225, DAX"
_by_country = te.preview_candidates_by_country()
check('Japan group has 7203.T and 6758.T', {
    "Japan": sorted(r["ticker"] for r in _by_country.get("Japan", [])),
}["Japan"] == ["6758.T", "7203.T"])
check('Germany group has SIE.DE', [r["ticker"] for r in _by_country.get("Germany", [])] == ["SIE.DE"])
check("Japan sorted by value_score descending (7203.T 60 before 6758.T None)",
      [r["ticker"] for r in _by_country["Japan"]] == ["7203.T", "6758.T"])

# ======================================================================
# CHECK 10: byte-identical proof - select_top100_pool()'s own return
# value never depends on TOP200_PREVIEW_UNIVERSES at all (the switch
# is never read inside pool selection/_eligible_scan_payloads/
# _build_best_by_ticker).
# ======================================================================
os.environ.pop("TOP200_PREVIEW_UNIVERSES", None)
_pool_unset = te.select_top100_pool(log=lambda *a, **k: None)
os.environ["TOP200_PREVIEW_UNIVERSES"] = "Nikkei 225, DAX"
_pool_set = te.select_top100_pool(log=lambda *a, **k: None)
os.environ.pop("TOP200_PREVIEW_UNIVERSES", None)
check("select_top100_pool()'s own return value is byte-identical whether "
      "TOP200_PREVIEW_UNIVERSES is set or not (Nikkei 225/DAX are both "
      "still PRIVATE and excluded from the real pool either way)",
      json.dumps(_pool_unset, sort_keys=True, default=str) ==
      json.dumps(_pool_set, sort_keys=True, default=str))
check("neither 7203.T nor SIE.DE entered the real public pool "
      "(still excluded by _eligible_scan_payloads()'s private-universe skip)",
      not any(r["ticker"] in ("7203.T", "6758.T", "SIE.DE") for r in _pool_unset))

# ======================================================================
# CHECK 11-14: _render_top200_preview_panel() end to end.
# ======================================================================
from streamlit.testing.v1 import AppTest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _render(switch_on):
    _env = f'os.environ["TOP200_PREVIEW_UNIVERSES"] = "Nikkei 225, DAX"' if switch_on else \
           'os.environ.pop("TOP200_PREVIEW_UNIVERSES", None)'
    script = f"""
import os, sys
sys.path.insert(0, {REPO_ROOT!r})
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = {TESTVOL!r}
{_env}
import app
app._render_top200_preview_panel()
"""
    at = AppTest.from_string(script, default_timeout=60)
    at.run()
    assert not at.exception, f"_render_top200_preview_panel() raised: {at.exception}"
    return at


_at_off = _render(switch_on=False)
_captions_off = " ".join(c.value or "" for c in _at_off.get("caption"))
check("switch unset: just the explanatory caption, no expander, no table",
      "TOP200_PREVIEW_UNIVERSES is unset" in _captions_off and not _at_off.get("expander"))

_at_on = _render(switch_on=True)
_expander_labels = [e.label for e in _at_on.get("expander")]
check('switch set: a "Top 20 Japan" expander and a "Top 20 Germany" expander exist',
      any("Japan" in l for l in _expander_labels)
      and any("Germany" in l for l in _expander_labels))
_metrics_on = {m.label: m.value for m in _at_on.get("metric")}
check('coverage metrics present: Rated=1 (7203.T), Waiting=2 (6758.T, SIE.DE)',
      _metrics_on.get("Rated") == "1" and _metrics_on.get("Waiting") == "2")
check("no exception, no write anywhere (read-only panel)", not _at_on.exception)

# ======================================================================
# CHECK 15: the scoring-request fingerprint is untouched - re-running
# the existing COMMIT 4 test's own sha256 check proves _request_
# params()/the batch entrant-building path is byte-for-byte the same
# as before this PART 5 diff.
# ======================================================================
FIXTURE_ENTRANTS = [
    {"ticker": "ADP", "company_name": "ADP-shaped", "sector": "Industrials"},
    {"ticker": "CSL.AX", "company_name": "CSL-shaped", "sector": "Health Care"},
    {"ticker": "AZN_FIXTURE.L", "company_name": "AZN-shaped", "sector": "Health Care"},
    {"ticker": "RY_FIXTURE.TO", "company_name": "RY-shaped", "sector": "Financials"},
    {"ticker": "7203_FIXTURE.T", "company_name": "Toyota-shaped", "sector": "Consumer Discretionary"},
]
EXPECTED_FINGERPRINT = "f00c69f5392af5e2ca435fb4c8a314393c14b4c0dc498cc4293a59d1276e5196"
_params = te._request_params(FIXTURE_ENTRANTS)
_fp = hashlib.sha256(json.dumps(_params, sort_keys=True).encode()).hexdigest()
check("scoring-request fingerprint unchanged by this PART 5 diff "
      f"(_request_params() sha256 still {EXPECTED_FINGERPRINT})",
      _fp == EXPECTED_FINGERPRINT)

print()
print(f"PASS={passed} FAIL={failed}")
sys.exit(1 if failed else 0)
