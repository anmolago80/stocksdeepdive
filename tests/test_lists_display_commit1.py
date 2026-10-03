"""
Lists & display, Commit 1 - real constituent lists for FTSE 100,
FTSE 250, TSX 60, TSX Composite (Director-directed, 3 Oct 2026).

ALL FIXTURES IN THIS FILE ARE SYNTHETIC - this sandbox has no outbound
network access, same disclosure as every other fixture-based test in
this repo.

Covers:
  - Each source of the new 4-step chain succeeding alone: Wikipedia
    only, fund-file only (with its equity-only filtering), last known-
    good list only, everything failing (-> not scanning).
  - Source order: when both Wikipedia and the fund file succeed,
    Wikipedia is the one actually used/saved, and the diff between the
    two is logged.
  - Last-good list persistence: saved after a live success, reused
    (with an age log line, escalating to a WARNING past 35 days) when
    every live source then fails.
  - The fund-file fetchers normalise through the market-correct
    symbol_mapping suffix (.L / .TO), not the US dash-for-dot rule.
  - Nikkei 225 / TOPIX 500 (Stage 1 Japan, no fund-file source) also
    get the same last-known-good persistence.
  - The Stage 1b integrity guards (FTSE 250 vs FTSE 100, TSX 60 vs TSX
    Composite) still pass - unaffected, since fetch_ftse100()/
    fetch_tsxcomposite() (the guards' own reference fetches) are
    unchanged raw Wikipedia-only functions.
  - Full regression suite green (run separately, not in this file).

Run: python3 tests/test_lists_display_commit1.py
"""
import io
import json
import logging
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

TESTVOL = tempfile.mkdtemp(prefix="lists_display_c1_test_")
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = TESTVOL
os.environ.pop("PRIVATE_UNIVERSES", None)
os.environ.pop("NIGHTLY_UNIVERSES", None)

import scanner_engine as se


class _CapturingHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append((record.levelname, record.getMessage()))

    def pop_all(self):
        out = list(self.records)
        self.records.clear()
        return out


_handler = _CapturingHandler()
se._log.addHandler(_handler)
se._log.setLevel(logging.INFO)


def _ishares_csv_fixture(tickers, extra_rows=None):
    """9 metadata lines (skiprows=9), then a header row with a Ticker
    column - same shape _clean_ishares_holdings_df()/_fetch_ishares_csv()
    already expect from a real iShares holdings-CSV export (see the
    Russell 2000/1000 fetchers' own fixtures-style usage elsewhere in
    this module)."""
    lines = ["meta"] * 9
    lines.append("Ticker,Name,Sector,Asset Class,Market Value,Weight (%)")
    for t in tickers:
        lines.append(f"{t},{t} PLC,Industrials,Equity,1000000,1.00")
    for extra in (extra_rows or []):
        lines.append(extra)
    return "\n".join(lines)


def _wiki_df(tickers):
    return pd.DataFrame({"Ticker": tickers, "Sector": ["Industrials"] * len(tickers)})


def _clear_last_good(slug):
    try:
        os.remove(se._universe_list_cache_path(slug))
    except OSError:
        pass


# ======================================================================
# T1: Wikipedia succeeds alone -> used, last-good saved.
# ======================================================================
_clear_last_good("ftse_100")
_wiki = _wiki_df(["AZN.L", "SHEL.L"])
with mock.patch.object(se, "fetch_ftse100", return_value=_wiki), \
     mock.patch.object(se, "fetch_ftse100_ishares", return_value=None):
    df, source = se.get_universe_pool("United Kingdom", "FTSE 100")
assert df is not None and set(df["Ticker"]) == {"AZN.L", "SHEL.L"}, df
assert source == "Wikipedia FTSE 100 (live)", source
_saved = se._load_last_good_universe_list("ftse_100")
assert _saved is not None and set(_saved["symbols"]) == {"AZN.L", "SHEL.L"}, _saved
assert _saved["source"] == "Wikipedia FTSE 100 (live)", _saved
print("[T1_wikipedia_alone] Wikipedia succeeds -> used verbatim, saved as the new last-good list OK")


# ======================================================================
# T2: Wikipedia fails, fund file succeeds alone -> fund file used,
# labelled as a fallback, also saved as the new last-good list.
# ======================================================================
_clear_last_good("ftse_250")
_fund_df = _wiki_df(["HWDN.L", "BBOX.L", "VTY.L"])
with mock.patch.object(se, "fetch_ftse250", return_value=None), \
     mock.patch.object(se, "fetch_ftse250_ishares", return_value=_fund_df):
    df, source = se.get_universe_pool("United Kingdom", "FTSE 250")
assert df is not None and set(df["Ticker"]) == {"HWDN.L", "BBOX.L", "VTY.L"}, df
assert "FTSE 250 unavailable" in source and "MIDD" in source, source
_saved = se._load_last_good_universe_list("ftse_250")
assert _saved is not None and set(_saved["symbols"]) == set(df["Ticker"]), _saved
print("[T2_fund_file_alone] Wikipedia fails, fund-file (source 2) succeeds -> used, labelled "
      "as a fallback, also saved as the new last-good list OK")


# ======================================================================
# T3: fund-file fetchers filter to equity-only rows (cash/escrow "-"
# dropped) and normalise through the MARKET-correct suffix, not the US
# dash-for-dot rule.
# ======================================================================
se.fetch_tsx60_ishares.clear()
_csv = _ishares_csv_fixture(["RY", "TD", "ENB"], extra_rows=["CASH,USD Cash,Cash,Cash,500,0.10", "-,Escrow,Cash,Cash,10,0.00"])
with mock.patch.object(se, "_fetch_ishares_csv", return_value=_csv):
    df = se.fetch_tsx60_ishares()
assert df is not None, df
assert set(df["Ticker"]) == {"RY.TO", "TD.TO", "ENB.TO"}, df
print("[T3_fund_file_filter] iShares XIU fixture with CASH/escrow rows -> 3 equity tickers kept, "
      "normalised to .TO (not the US dash-for-dot rule) OK")

se.fetch_ftse100_ishares.clear()
_csv2 = _ishares_csv_fixture(["AZN", "SHEL", "HSBA"])
with mock.patch.object(se, "_fetch_ishares_csv", return_value=_csv2):
    df2 = se.fetch_ftse100_ishares()
assert df2 is not None and set(df2["Ticker"]) == {"AZN.L", "SHEL.L", "HSBA.L"}, df2
print("[T3_fund_file_lse] iShares ISF fixture -> normalised to .L OK")


# ======================================================================
# T4: BOTH Wikipedia and fund file succeed -> Wikipedia is the one
# used/saved (fund file is cross-check only), and the diff between the
# two is logged in the exact format the task's own instruction specifies.
# ======================================================================
_clear_last_good("tsx_60")
_handler.pop_all()
_wiki60 = _wiki_df(["RY.TO", "TD.TO", "ENB.TO"])
_fund60 = _wiki_df(["RY.TO", "TD.TO", "BMO.TO"])  # one ticker differs each way
with mock.patch.object(se, "fetch_tsx60", return_value=_wiki60), \
     mock.patch.object(se, "fetch_tsx60_ishares", return_value=_fund60):
    df, source = se.get_universe_pool("Canada", "TSX 60")
assert source == "Wikipedia TSX 60 (live)", source
assert set(df["Ticker"]) == {"RY.TO", "TD.TO", "ENB.TO"}, "Wikipedia list must be the one actually used"
_diff_lines = [msg for lvl, msg in _handler.pop_all() if "wikipedia 3, fund file 3, in one not the other: 2" in msg]
assert _diff_lines, "expected a [universe] TSX 60: wikipedia N, fund file N, in one not the other: N diff log line"
assert "ENB.TO" in _diff_lines[0] and "BMO.TO" in _diff_lines[0], _diff_lines[0]
print(f"[T4_both_succeed_diff_logged] both sources succeed -> Wikipedia used, diff logged: "
      f"{_diff_lines[0]!r} OK")


# ======================================================================
# T5: last-good list reused when every live source fails; age logged,
# escalating to a WARNING past 35 days.
# ======================================================================
_clear_last_good("ftse_100")
se._save_last_good_universe_list("ftse_100", ["AZN.L", "SHEL.L"], "Wikipedia FTSE 100 (live)")
_handler.pop_all()
with mock.patch.object(se, "fetch_ftse100", return_value=None), \
     mock.patch.object(se, "fetch_ftse100_ishares", return_value=None):
    df, source = se.get_universe_pool("United Kingdom", "FTSE 100")
assert df is not None and set(df["Ticker"]) == {"AZN.L", "SHEL.L"}, df
assert "(saved list, " in source, source  # Director addendum 2 Part 2 item D relabel
_fresh_logs = [msg for lvl, msg in _handler.pop_all() if "using last known-good list" in msg]
assert _fresh_logs and "day(s) old" in _fresh_logs[0] and "older than" not in _fresh_logs[0], _fresh_logs
print(f"[T5a_last_good_fresh] both live sources fail -> fresh last-good list served, age logged "
      f"without a staleness warning: {_fresh_logs[0]!r} OK")

# Back-date the saved file's fetched_at to 40 days ago -> same path,
# but now a WARNING naming the 35-day threshold.
_path = se._universe_list_cache_path("ftse_100")
with open(_path) as f:
    _payload = json.load(f)
_payload["fetched_at"] = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat()
with open(_path, "w") as f:
    json.dump(_payload, f)
_handler.pop_all()
with mock.patch.object(se, "fetch_ftse100", return_value=None), \
     mock.patch.object(se, "fetch_ftse100_ishares", return_value=None):
    df, source = se.get_universe_pool("United Kingdom", "FTSE 100")
assert df is not None, df
_stale_warnings = [(lvl, msg) for lvl, msg in _handler.pop_all() if "using last known-good list" in msg]
assert _stale_warnings, "expected a last-good log line"
assert _stale_warnings[0][0] == "WARNING" and "older than 35 days" in _stale_warnings[0][1], _stale_warnings
print(f"[T5b_last_good_stale] a 40-day-old last-good list -> served, but now WARNING-level and "
      f"naming the 35-day threshold: {_stale_warnings[0][1]!r} OK")


# ======================================================================
# T6: everything fails (no Wikipedia, no fund file, no last-good at
# all) -> None, not scanning, with the exact required log line format.
# ======================================================================
_clear_last_good("ftse_250")
_handler.pop_all()
with mock.patch.object(se, "fetch_ftse250", return_value=None), \
     mock.patch.object(se, "fetch_ftse250_ishares", return_value=None):
    df, source = se.get_universe_pool("United Kingdom", "FTSE 250")
assert df is None, df
assert "not scanning" in source, source
_unavailable = [msg for lvl, msg in _handler.pop_all()
                if msg.startswith("[universe] FTSE 250: constituent list unavailable (") and msg.endswith("- not scanning")]
assert _unavailable, "expected the exact '[universe] FTSE 250: constituent list unavailable (<reason>) - not scanning' line"
print(f"[T6_everything_fails] no source available at all -> None, not scanning, exact log line: "
      f"{_unavailable[0]!r} OK")


# ======================================================================
# T7: Nikkei 225 / TOPIX 500 also get last-known-good persistence (no
# fund-file source for either, per Stage 1 Japan's own "never build a
# constituent list from memory" rule - still in force, just now backed
# by a real dated last-good list instead of an outright skip whenever
# one exists).
# ======================================================================
_clear_last_good("nikkei_225")
_nikkei_df = _wiki_df(["7203.T", "6758.T"])
with mock.patch.object(se, "fetch_nikkei225", return_value=_nikkei_df):
    df, source = se.get_universe_pool("Japan", "Nikkei 225")
assert df is not None and source == "Wikipedia Nikkei 225 (live)", (df, source)
assert se._load_last_good_universe_list("nikkei_225") is not None
with mock.patch.object(se, "fetch_nikkei225", return_value=None):
    df2, source2 = se.get_universe_pool("Japan", "Nikkei 225")
assert df2 is not None and set(df2["Ticker"]) == {"7203.T", "6758.T"}, df2
assert "(saved list, " in source2, source2  # Director addendum 2 Part 2 item D relabel
print("[T7a_nikkei_last_good] Nikkei 225 - live success saves a last-good list; a later failed "
      "fetch reuses it instead of skipping OK")

_clear_last_good("topix_500")
_topix_df = _wiki_df([f"{9000+i}.T" for i in range(400)])
with mock.patch.object(se, "fetch_topix500", return_value=_topix_df):
    df3, source3 = se.get_universe_pool("Japan", "TOPIX 500")
assert df3 is not None and len(df3) == 400, df3
assert se._load_last_good_universe_list("topix_500") is not None
with mock.patch.object(se, "fetch_topix500", return_value=None):
    df4, source4 = se.get_universe_pool("Japan", "TOPIX 500")
assert df4 is not None and len(df4) == 400, df4
assert "(saved list, " in source4, source4  # Director addendum 2 Part 2 item D relabel
# Still never a fallback list when there is truly nothing saved either.
_clear_last_good("topix_500")
with mock.patch.object(se, "fetch_topix500", return_value=None):
    df5, source5 = se.get_universe_pool("Japan", "TOPIX 500")
assert df5 is None and "not scanning" in source5, (df5, source5)
print("[T7b_topix_last_good] TOPIX 500 - same last-good reuse; with nothing saved at all, still "
      "None/not scanning, never a fallback list OK")


# ======================================================================
# T8: Stage 1b's own integrity guards (FTSE 250 vs FTSE 100 sibling
# check, TSX 60 vs TSX Composite subset check) still pass unaffected -
# their own reference fetches (fetch_ftse100()/fetch_tsxcomposite())
# are unchanged raw Wikipedia-only functions, never touched by this
# commit's new chain.
# ======================================================================
_FTSE100_REF = pd.DataFrame({"Ticker": ["AAA.L", "BBB.L"], "Sector": ["Financials", "Energy"]})
with mock.patch.object(se, "fetch_ftse100", return_value=_FTSE100_REF):
    ok, reason = se.verify_universe_before_save("FTSE 250", {"CCC.L", "DDD.L"}, log=None)
    assert ok is True, reason
    ok2, reason2 = se.verify_universe_before_save("FTSE 250", {"CCC.L", "AAA.L"}, log=None)
    assert ok2 is False and "AAA.L" in reason2, (ok2, reason2)

_TSX_COMPOSITE_REF = pd.DataFrame({"Ticker": ["RY.TO", "TD.TO", "ENB.TO", "SHOP.TO"], "Sector": ["Financials"] * 4})
with mock.patch.object(se, "fetch_tsxcomposite", return_value=_TSX_COMPOSITE_REF):
    ok3, reason3 = se.verify_universe_before_save("TSX 60", {"RY.TO", "TD.TO"}, log=None)
    assert ok3 is True, reason3
    ok4, reason4 = se.verify_universe_before_save("TSX 60", {"RY.TO", "XYZ.TO"}, log=None)
    assert ok4 is False and "XYZ.TO" in reason4, (ok4, reason4)
print("[T8_integrity_guards_unaffected] FTSE 250 sibling check and TSX 60 subset check still "
      "pass, unaffected by the new fund-file/last-good chain OK")


print("\nALL Lists & display Commit 1 CHECKS PASSED")
