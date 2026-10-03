"""
Stage 1 Japan, Commit B - Nikkei 225 and TOPIX 500, private (Director-
directed, 3 Oct 2026).

ALL FIXTURES IN THIS FILE ARE SYNTHETIC - this sandbox has no outbound
network access, same disclosure as every other fixture-based test in
this repo.

Covers:
  - fetch_nikkei225() against a saved Wikipedia-shaped HTML fixture;
    fetch_topix500() against a saved JPX-file-shaped CSV fixture, the
    Core30+Large70+Mid400 filter, and the "list unavailable -> no
    scan, last known good kept" behaviour.
  - Nikkei 225 / TOPIX 500 added to PRIVATE_UNIVERSES' code default -
    the Stage 1b privacy byte-identical test extended to these two
    universes, and extended BEYOND select_top100_pool() to the
    Scanner universe list, the write-side gate that protects the
    homepage teaser (snapshot_store contents), and the digest/alert
    input - each compared with and without the private scan files.
  - The manual "Rescan now (owner)" trigger's own privacy inheritance
    (B3 - its option list is exempt from privacy by construction, but
    the side effects a rescan triggers must still respect every rule).
  - Scheduler before/after: the two new entries on Monday/Friday, all
    16 pre-existing entries (12 original + Stage 1b's own 4) unchanged.
  - Full regression suite green (run separately, not in this file).

Run: python3 tests/test_stage1_japan_commit_b.py
"""
import contextlib
import io
import os
import sys
import tempfile
import threading
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

TESTVOL = tempfile.mkdtemp(prefix="japan_commit_b_test_")
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = TESTVOL
os.environ.pop("PRIVATE_UNIVERSES", None)
os.environ.pop("NIGHTLY_UNIVERSES", None)

import scanner_engine as se
import scan_store
import scheduler_engine as sched
import nightly_scan
import top100_engine
import symbol_mapping


# ======================================================================
# T1: fetch_nikkei225() against a saved Wikipedia-shaped HTML fixture.
# ======================================================================
def _nikkei_bulleted_list_html(codes):
    # Director addendum 2, Part 2, item 2 (3 Oct 2026, verified directly
    # from the live page): Nikkei 225's real constituents are bulleted
    # list items ("Honda Motor Co., Ltd. (TYO: 7267)"), not a table -
    # see scanner_engine.fetch_nikkei225()'s own docstring. This fixture
    # replaces the old (wrong) HTML-<table> shape this test used before
    # that fix.
    return "<html><body><ul>" + "".join(
        f"<li>Company {c} Co., Ltd. (TYO: {c})</li>" for c in codes
    ) + "</ul></body></html>"


class _FakeNikkeiResp:
    def __init__(self, text):
        self.status_code = 200
        self.text = text
        self.content = text.encode()
        self.headers = {"Content-Type": "text/html; charset=UTF-8"}

    def raise_for_status(self):
        pass


_nikkei_codes = [f"{1000 + i}" for i in range(225)]
_nikkei_fixture_html = _nikkei_bulleted_list_html(_nikkei_codes)

with mock.patch.object(se.requests, "get", return_value=_FakeNikkeiResp(_nikkei_fixture_html)):
    se.fetch_nikkei225.clear()
    df_nikkei = se.fetch_nikkei225()
assert df_nikkei is not None and len(df_nikkei) == 225, df_nikkei
assert set(df_nikkei["Ticker"]) == {f"{c}.T" for c in _nikkei_codes}
print(f"[T1_fetch_nikkei225] 225-row Wikipedia-shaped fixture -> {len(df_nikkei)} tickers, "
      "all normalised to the .T suffix OK")


# ======================================================================
# T1: fetch_topix500() against a saved JPX-file-shaped CSV fixture -
# Core30/Large70/Mid400 kept, Small1/Small2 filtered out.
# ======================================================================
def _topix_csv_fixture_cp932(n_core30, n_large70, n_mid400, n_small):
    # Director addendum 2, Part 2, item 2 (3 Oct 2026, verified directly
    # from the live file): topixweight_j.csv has Japanese columns コード/
    # 銘柄名/ニューインデックス区分 (values "TOPIX Core30" etc), Shift-
    # JIS/cp932 encoded - see scanner_engine.fetch_topix500()'s own
    # docstring. Replaces the old (wrong) English-column/UTF-8 shape
    # this test used before that fix.
    rows = []
    code = 2000
    for _ in range(n_core30):
        rows.append((code, "TOPIX Core30")); code += 1
    for _ in range(n_large70):
        rows.append((code, "TOPIX Large70")); code += 1
    for _ in range(n_mid400):
        rows.append((code, "TOPIX Mid400")); code += 1
    for _ in range(n_small):
        rows.append((code, "TOPIX Small 1")); code += 1
    lines = ["コード,銘柄名,ニューインデックス区分"]
    lines += [f"{c},テスト銘柄{c},{g}" for c, g in rows]
    return "\n".join(lines).encode("cp932")


_TOPIX_INDEX_PAGE_HTML = (
    '<html><body><a href="/markets/indices/topix/tvdivq0000006pov-att/'
    'topixweight_j.csv">構成銘柄別ウエイト一覧</a></body></html>'
)


class _FakeTopixPageResp:
    def __init__(self, status_code, text):
        self.status_code = status_code
        self.text = text
        self.content = text.encode("utf-8")
        self.headers = {"Content-Type": "text/html; charset=UTF-8"}

    def raise_for_status(self):
        pass


class _FakeTopixCsvResp:
    def __init__(self, status_code, content_bytes):
        self.status_code = status_code
        self.content = content_bytes
        self.headers = {"Content-Type": "application/octet-stream"}


def _topix_get_fixture(csv_bytes):
    def _get(url, headers=None, timeout=None):
        if url == se.TOPIX_INDEX_PAGE_URL:
            return _FakeTopixPageResp(200, _TOPIX_INDEX_PAGE_HTML)
        return _FakeTopixCsvResp(200, csv_bytes)
    return _get


_topix_csv_ok = _topix_csv_fixture_cp932(30, 70, 400, 50)
with mock.patch.object(se, "requests") as _mreq:
    _mreq.get.side_effect = _topix_get_fixture(_topix_csv_ok)
    se.fetch_topix500.clear()
    df_topix = se.fetch_topix500()
assert df_topix is not None and len(df_topix) == 500, df_topix
print(f"[T1_fetch_topix500_filter] 550-row JPX-shaped fixture (30 Core30 + 70 Large70 "
      f"+ 400 Mid400 + 50 Small) -> {len(df_topix)} tickers kept, Small rows dropped OK")

# "list unavailable -> no scan, last known good kept": a failed fetch
# returns None - never a fallback list (see fetch_topix500()'s own
# docstring) - and get_universe_pool() passes that straight through,
# which nightly_scan.run_universe_scan() already treats as "no tickers
# resolved" (no save_scan() call at all, so whatever's on disk stays).
with mock.patch.object(se, "requests") as _mreq:
    _mreq.get.return_value = _FakeTopixPageResp(500, "")
    se.fetch_topix500.clear()
    df_topix_fail = se.fetch_topix500()
assert df_topix_fail is None, df_topix_fail
pool_df, source = se.get_universe_pool("Japan", "TOPIX 500")
assert pool_df is None and "not scanning" in source, (pool_df, source)
print(f"[T1_fetch_topix500_unavailable] a failed JPX fetch returns None (source={source!r}) - "
      "no static fallback, never built from memory OK")

# Save a "last known good" TOPIX 500 scan, then prove a failed re-fetch
# never overwrites it (nightly_scan.run_universe_scan()'s own "no
# tickers resolved -> return None, no save_scan() call" behaviour).
_existing_rows = [{"Ticker": "2000.T", "Company Name": "Existing Co"}]
scan_store.save_scan("TOPIX 500", _existing_rows, "fixture")
with mock.patch.object(se, "get_universe_pool", return_value=(None, "JPX constituents file unavailable - not scanning")):
    result = nightly_scan.run_universe_scan("TOPIX 500", log=lambda *a, **k: None)
assert result is None, result
_kept = scan_store.load_scan_raw("TOPIX 500", allow_private=True)
assert _kept is not None and _kept["rows"] == _existing_rows, _kept
print("[T1_keep_last_known_good] a failed re-fetch never overwrites TOPIX 500's "
      "last known good saved scan OK")


# ======================================================================
# T2: Nikkei 225 / TOPIX 500 are private by default.
# ======================================================================
os.environ.pop("PRIVATE_UNIVERSES", None)
assert scan_store.is_private_universe("Nikkei 225") is True
assert scan_store.is_private_universe("TOPIX 500") is True
print("[T2_is_private] Nikkei 225/TOPIX 500 private by default OK")


# ======================================================================
# T2: Scanner universe list - default DENY.
# ======================================================================
scan_store.save_scan("Nikkei 225", [{"Ticker": "7203.T", "Company Name": "Toyota-shaped"}], "fixture")
scan_store.save_scan("TOPIX 500", _existing_rows, "fixture")
assert "Nikkei 225" not in scan_store.list_saved_universes()
assert "TOPIX 500" not in scan_store.list_saved_universes()
assert "Nikkei 225" in scan_store.list_saved_universes(include_private=True)
assert "TOPIX 500" in scan_store.list_saved_universes(include_private=True)
print("[T2_scanner_list] the Scanner universe list excludes Nikkei 225/TOPIX 500 by "
      "default, includes them with include_private=True OK")


# ======================================================================
# T2: write-side gate - snapshot_store contents (protects the homepage
# teaser, since _home_top5_by_country() reads ONLY snapshot_store.
# all_public_rows()) and the digest/alert input - extends Stage 1b's
# own _run_nightly() private-universe gate test to Nikkei 225.
# ======================================================================
_fake_payload = {
    "rows": [{"Ticker": "7203.T", "Company Name": "Toyota-shaped"}],
    "attention_lite": False,
}
_calls = {"snapshot": 0, "alert": 0, "insider": 0}

with mock.patch.object(nightly_scan, "run_universe_scan", return_value=_fake_payload), \
     mock.patch.object(nightly_scan, "refresh_market_cap_ranking", return_value=None), \
     mock.patch.object(sched, "_load_state", return_value={}), \
     mock.patch.object(sched, "_save_state", return_value=None):
    import alert_engine
    import snapshot_store
    import insider_engine
    with mock.patch.object(alert_engine, "snapshot_previous_values", return_value={}), \
         mock.patch.object(alert_engine, "check_universe_rows",
                            side_effect=lambda *a, **k: _calls.__setitem__("alert", _calls["alert"] + 1)), \
         mock.patch.object(snapshot_store, "build_snapshots_from_scan",
                            side_effect=lambda *a, **k: _calls.__setitem__("snapshot", _calls["snapshot"] + 1)), \
         mock.patch.object(insider_engine, "refresh_universe",
                            side_effect=lambda *a, **k: _calls.__setitem__("insider", _calls["insider"] + 1)):
        sched._run_nightly({"universes": ["Nikkei 225"]}, log=lambda *a, **k: None, run_night="2026-10-05")

assert _calls["snapshot"] == 0, _calls
assert _calls["alert"] == 0, _calls
assert _calls["insider"] == 1, _calls
print("[T2_run_nightly_gate] _run_nightly() on Nikkei 225 never calls snapshot_store."
      "build_snapshots_from_scan() (so all_public_rows()/the homepage teaser can never "
      "see it) or alert_engine.check_universe_rows() (so no digest/alert input either) - "
      "insider_engine.refresh_universe() still runs (left ungated) OK")


# ======================================================================
# T3 (B3): the manual "Rescan now (owner)" trigger inherits the same
# privacy rule - a queued rescan of Nikkei 225 must not produce a
# public snapshot or alert input either, since _run_queued_rescan_tick()
# delegates straight to the same _run_nightly() just proven gated above.
# ======================================================================
sched._queue_rescan_request(["Nikkei 225"], requested_by="owner@x.com", log=lambda *a: None)
_rescan_calls = {"snapshot": 0, "alert": 0, "insider": 0}

with mock.patch.object(nightly_scan, "run_universe_scan", return_value=_fake_payload), \
     mock.patch.object(nightly_scan, "refresh_market_cap_ranking", return_value=None):
    with mock.patch.object(alert_engine, "snapshot_previous_values", return_value={}), \
         mock.patch.object(alert_engine, "check_universe_rows",
                            side_effect=lambda *a, **k: _rescan_calls.__setitem__("alert", _rescan_calls["alert"] + 1)), \
         mock.patch.object(snapshot_store, "build_snapshots_from_scan",
                            side_effect=lambda *a, **k: _rescan_calls.__setitem__("snapshot", _rescan_calls["snapshot"] + 1)), \
         mock.patch.object(insider_engine, "refresh_universe",
                            side_effect=lambda *a, **k: _rescan_calls.__setitem__("insider", _rescan_calls["insider"] + 1)):
        sched._run_queued_rescan_tick(
            {"universes": [], "universe_cadence": {}}, lambda *a, **k: None,
            rl_cooldown_active=False, rl_cooldown_until=None,
            abandoned_status=None, abandoned_info=None,
        )

assert _rescan_calls["snapshot"] == 0, _rescan_calls
assert _rescan_calls["alert"] == 0, _rescan_calls
assert _rescan_calls["insider"] == 1, _rescan_calls
status = sched._rescan_request_status()
assert status["last_run"]["universes"] == ["Nikkei 225"], status
print("[T3_manual_rescan_gate] a manually-queued rescan of Nikkei 225 (the owner's own "
      "'Rescan now' control) still never produces a public snapshot or alert input - it "
      "delegates to the same gated _run_nightly() OK")

# B3's other half: the option list itself is EXEMPT from privacy -
# scheduler_engine._cfg()["universe_cadence"] (what _render_rescan_now_
# control() reads) carries no is_private_universe() filtering at all,
# so both new universes appear in it (confirmed by direct code read -
# see this task's own report).
_cadence = sched._cfg()["universe_cadence"]
assert "Nikkei 225" in _cadence and "TOPIX 500" in _cadence, _cadence
print("[T3_rescan_option_list_exempt] Nikkei 225/TOPIX 500 appear in the Rescan-now "
      "option list (scheduler_engine._cfg()['universe_cadence'], never filtered by "
      "privacy) OK")


# ======================================================================
# T4: byte-identical selection - select_top100_pool() with Nikkei 225's
# saved scan file present (default PRIVATE_UNIVERSES, excluded) is
# IDENTICAL to a run where that file doesn't exist at all; reveals a
# private-only ticker when PRIVATE_UNIVERSES="".
# ======================================================================
_PUB_ROW = {
    "Ticker": "PUB.AX", "Company Name": "Public Co", "Price": 10.0,
    "Intrinsic Value": 11.0, "MOS %": 9.0, "Long Score": 55.0, "Quality": 50.0,
    "Psychology": 50.0, "Discovery (lite)": 50.0, "Sector": "Industrials",
}
_NIKKEI_ONLY_ROW = {
    "Ticker": "9999.T", "Company Name": "Nikkei-only Co", "Price": 1000.0,
    "Intrinsic Value": 1150.0, "MOS %": 13.0, "Long Score": 95.0, "Quality": 90.0,
    "Psychology": 90.0, "Discovery (lite)": 90.0, "Sector": "Technology",
}
scan_store.save_scan("ASX 200", [_PUB_ROW], "fixture")
scan_store.save_scan("Nikkei 225", [_NIKKEI_ONLY_ROW], "fixture")


def _run_pool():
    rows = top100_engine.select_top100_pool(log=lambda *a, **k: None)
    return sorted(r["ticker"] for r in rows)


os.environ.pop("PRIVATE_UNIVERSES", None)
with_private_file = _run_pool()
assert "9999.T" not in with_private_file, with_private_file

_nikkei_path = scan_store._path("Nikkei 225")
_saved_bytes = open(_nikkei_path, "rb").read()
os.remove(_nikkei_path)
without_private_file = _run_pool()
assert with_private_file == without_private_file, (with_private_file, without_private_file)
print(f"[T4_byte_identical] select_top100_pool() output is IDENTICAL "
      f"({len(with_private_file)} ticker(s)) whether Nikkei 225's saved scan file "
      "exists (default, excluded) or doesn't exist at all OK")

with open(_nikkei_path, "wb") as f:
    f.write(_saved_bytes)

os.environ["PRIVATE_UNIVERSES"] = ""
revealed = _run_pool()
assert "9999.T" in revealed, revealed
os.environ.pop("PRIVATE_UNIVERSES", None)
print("[T4_revealed] with PRIVATE_UNIVERSES='' the Nikkei-only ticker DOES appear in "
      "select_top100_pool()'s output OK")


# ======================================================================
# T5: scheduler before/after - Nikkei 225 (mon)/TOPIX 500 (fri) added,
# all 16 pre-existing entries (Stage 1b's own 12+4) unchanged.
# ======================================================================
_BEFORE_NIGHTLY_UNIVERSES = (
    "ASX 200:daily, ASX 300:daily, ASX All Technology:daily, S&P 500:daily, "
    "Nasdaq 100:daily, Dow Jones 30:daily, All Ordinaries:mon, "
    "S&P 400 MidCap:tue, Small Caps (S&P 600):wed, Russell 1000:thu, "
    "S&P 500 Dividend Aristocrats:fri, Russell 2000:sat, "
    "FTSE 100:sun, FTSE 250:sun, TSX 60:tue, TSX Composite:tue"
)
_before_map = sched._parse_nightly_universes(_BEFORE_NIGHTLY_UNIVERSES)
_after_map = sched._parse_nightly_universes(sched._DEFAULT_NIGHTLY_UNIVERSES)

print("[T5_schedule] universe                before    after")
for u, cadence in _before_map.items():
    after_cadence = _after_map.get(u)
    print(f"    {u:28s} {cadence:8s} {after_cadence}")
    assert after_cadence == cadence, (u, cadence, after_cadence)
_new_entries = {u: c for u, c in _after_map.items() if u not in _before_map}
for u, cadence in _new_entries.items():
    print(f"    {u:28s} {'(new)':8s} {cadence}")
assert _new_entries == {"Nikkei 225": "mon", "TOPIX 500": "fri"}, _new_entries
assert len(_before_map) == 16, _before_map
print("[T5_schedule] all 16 pre-existing entries unchanged; Nikkei 225 lands on mon, "
      "TOPIX 500 on fri, exactly as specified OK")


# ======================================================================
# T6: Tokyo quote recorder window - 09:00-15:30 Asia/Tokyo, skipping
# 11:30-12:30 lunch (already proven correct via a direct smoke test
# during development; re-asserted here as part of this commit's own
# suite).
# ======================================================================
import quote_recorder as qr
from datetime import datetime, timezone

assert qr.is_due_now(qr.MARKET_JP, now=datetime(2026, 10, 5, 1, 0, tzinfo=timezone.utc))   # 10:00 JST
assert not qr.is_due_now(qr.MARKET_JP, now=datetime(2026, 10, 5, 3, 0, tzinfo=timezone.utc))  # 12:00 JST (lunch)
assert qr.is_due_now(qr.MARKET_JP, now=datetime(2026, 10, 5, 5, 0, tzinfo=timezone.utc))   # 14:00 JST
assert not qr.is_due_now(qr.MARKET_JP, now=datetime(2026, 10, 5, 7, 0, tzinfo=timezone.utc))  # 16:00 JST (after close)
print("[T6_jp_recorder_window] 09:00-15:30 Asia/Tokyo, skipping the 11:30-12:30 lunch "
      "break OK")


# ======================================================================
# T7: Nikkei 225 / TOPIX 500 overlap logging - informational only, no
# subset guard.
# ======================================================================
_overlap_logs = []
with mock.patch.object(se, "fetch_nikkei225", return_value=pd.DataFrame({"Ticker": ["7203.T", "6758.T"]})), \
     mock.patch.object(se, "fetch_topix500", return_value=pd.DataFrame({"Ticker": ["7203.T", "9999.T"]})):
    se.log_nikkei_topix_overlap(log=_overlap_logs.append)
assert any("overlap: 1 ticker" in l for l in _overlap_logs), _overlap_logs
assert "TOPIX 500" not in se.UNIVERSE_INTEGRITY_TRACKED_UNIVERSES
assert "Nikkei 225" not in se.UNIVERSE_INTEGRITY_TRACKED_UNIVERSES
print("[T7_overlap_log] Nikkei 225 / TOPIX 500 overlap count logged (1 ticker in this "
      "fixture); neither carries a subset-guard entry in "
      "UNIVERSE_INTEGRITY_TRACKED_UNIVERSES, per this task's own 'no subset guard "
      "required' instruction OK")

print("\nAll Stage 1 Japan Commit B checks passed.")
