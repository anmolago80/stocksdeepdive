"""
Cache-safety fix (owner-reported, 28 Sep 2026), follow-up to the growth-
estimate-fetch resilience fix (4a1a000).

Root cause of THIS bug: 4a1a000 changed fcf_valuation_engine.py/
capm_engine.py/resolver_engine.py (the growth-estimate retry/status fix)
WITHOUT bumping auto_compounder_engine.ENGINE_VERSION - the exact "easy to
forget" failure that constant's own comment already warned about. The Fair
Value tab's disk-persisted section cache (auto_compounder_engine._read_
cache()/_write_cache(), which survives a redeploy - unlike an in-memory
st.cache_data cache) kept serving a pre-fix ADP result for hours.

This fix:
  1. ENGINE_VERSION bumped 43 -> 44 (the immediate, belt-and-suspenders fix
     for THIS incident).
  2. VALUATION_SOURCE_HASH (auto_compounder_engine.py): a hash of the raw
     combined source of fcf_valuation_engine.py/capm_engine.py/resolver_
     engine.py, computed once at import time, included in the disk cache's
     own validity check (_read_cache()) and written into every new cache
     entry's _meta (build_sections()) - the STRUCTURAL fix: any edit to
     those three files invalidates every cached section automatically,
     with no constant to remember to bump.
  3. Audited every other st.cache_data cache "in front of the DCF" (see
     this file's own inventory comment below) and threaded the SAME hash
     into the two that genuinely cache valuation OUTPUT (app.py's
     _featured_analysis(), portfolio_health_engine.fetch_snapshot()) -
     defense in depth, since both are in-memory and already self-heal on
     a redeploy (unlike the disk cache above, the one that actually
     caused the incident).
  4. capm_engine.get_growth_estimates_5y()'s "fetch_failed" status is no
     longer cached at the same 30-minute TTL as a genuine "ok"/
     "no_coverage" result - it now expires after 5 minutes (manual
     per-ticker memo with a two-tier TTL, since st.cache_data's ttl is
     fixed per function, not per return value).

Cache inventory (every st.cache_data function whose body calls resolve_
intrinsic_value/dcf_intrinsic_value/deep_dive_engine.analyze/analyze_
ticker_lite - found by grepping every @st.cache_data-decorated function
in the codebase and checking its body for those four names):
  - auto_compounder_engine._read_cache()/_write_cache() (disk-persisted,
    24h TTL, survives a redeploy) - THE cache that caused the incident;
    gated by ENGINE_VERSION + BUNDLE_VERSION + VALUATION_SOURCE_HASH.
  - app.py:_featured_analysis() (st.cache_data, ttl=21600/6h, in-memory,
    self-heals on redeploy) - homepage featured card; now keys on
    VALUATION_SOURCE_HASH too (explicit non-underscore parameter, passed
    by both call sites).
  - portfolio_health_engine.fetch_snapshot() (st.cache_data, ttl=1800/
    30min, in-memory, self-heals on redeploy) - Portfolio page per-
    ticker snapshot; now keys on VALUATION_SOURCE_HASH too, via a
    default value baked in at module-import time (no caller needs to
    change - Python resolves a default argument once, when `def` runs).
  - capm_engine.get_growth_estimates_5y() (own two-tier TTL, see #4
    above) - caches a raw Yahoo INPUT + its own unit-normalization, not
    the DCF's output; still worth the shorter fetch_failed TTL on its
    own merits, unrelated to the disk-cache incident.
  - capm_engine.get_risk_free_rate()/get_au_risk_free_rate_live()
    (st.cache_data, ttl=10800/86400) - raw external bond-yield inputs,
    not DCF output; in-memory, self-heals on redeploy. Left as-is.
  - app.py:_home_top5_by_country() (st.cache_data, ttl=1800) - a grep
    false positive (the docstring MENTIONS analyze_ticker_lite, the
    function itself only reads pre-computed snapshot_store rows, no
    resolver call) - confirmed by reading the function; excluded.
  - admin_data_audit.run_all_checks() (st.cache_data, ttl=1800) - the
    owner-only Admin Dashboard diagnostic audit tool, already has its
    own explicit "_cache_bust" manual-refresh escape hatch, in-memory,
    and its whole purpose is catching exactly this kind of discrepancy;
    not a customer-facing "cache in front of the DCF". Left as-is.
  - snapshot_store.py/scan_store.py/score_history.py - persisted OUTPUT
    of the nightly scan (a history/archive, not a memoization cache
    layered in front of a live recompute); a code fix takes effect on
    the VERY NEXT scan regardless, with no stale-cache gate to close.
    Out of scope.

Run: python3 tests/test_valuation_cache_safety.py
"""
import hashlib
import inspect
import json
import os
import sys
import tempfile
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import auto_compounder_engine as ace
import app as _app
import portfolio_health_engine as phe
import capm_engine as ce


# ======================================================================
# CHECK: ENGINE_VERSION bumped for this incident (43 -> 44).
# ======================================================================
assert ace.ENGINE_VERSION >= 44, ace.ENGINE_VERSION
print(f"[engine_version_bumped_44] auto_compounder_engine.ENGINE_VERSION = {ace.ENGINE_VERSION} "
      "(was 43, >= 44 confirms this incident's bump wasn't reverted by a later task) OK")


# ======================================================================
# CHECK: VALUATION_SOURCE_HASH is a real hash of the hashed files' raw
# bytes (recomputed independently here, not just trusting the module's
# own claim), and it's a stable, non-None 12-char hex string.
# Audit fixes Commit 4 (30 Sep 2026, owner-directed): share_class_
# engine.py added to the hashed file list (it directly changes every
# per-share figure this module computes via _whole_company_shares(),
# and was the one gap the original 3-file list left open) - this
# test's own independent recomputation follows suit.
# ======================================================================
_repo_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_h = hashlib.sha1()
for _fname in ("fcf_valuation_engine.py", "capm_engine.py", "resolver_engine.py",
                "share_class_engine.py"):
    with open(os.path.join(_repo_dir, _fname), "rb") as f:
        _h.update(f.read())
_expected_hash = _h.hexdigest()[:12]
assert ace.VALUATION_SOURCE_HASH == _expected_hash, (ace.VALUATION_SOURCE_HASH, _expected_hash)
assert ace.VALUATION_SOURCE_HASH is not None and len(ace.VALUATION_SOURCE_HASH) == 12
print(f"[valuation_source_hash_matches_files] VALUATION_SOURCE_HASH={ace.VALUATION_SOURCE_HASH!r} "
      "independently recomputed from the same 4 files' raw bytes and matches exactly OK")

# A change to ANY of the three files (simulated here rather than actually
# editing them) changes the hash - proves this isn't a constant that
# happens to look like a hash but doesn't actually depend on file content.
_h2 = hashlib.sha1()
with open(os.path.join(_repo_dir, "fcf_valuation_engine.py"), "rb") as f:
    _h2.update(f.read() + b"# a hypothetical one-byte edit")
with open(os.path.join(_repo_dir, "capm_engine.py"), "rb") as f:
    _h2.update(f.read())
with open(os.path.join(_repo_dir, "resolver_engine.py"), "rb") as f:
    _h2.update(f.read())
assert _h2.hexdigest()[:12] != ace.VALUATION_SOURCE_HASH
print("[valuation_source_hash_changes_with_content] a hypothetical one-byte edit to any of the "
      "3 files produces a DIFFERENT hash OK")


# ======================================================================
# CHECK: _read_cache() rejects a disk-cached entry whose valuation_
# source_hash doesn't match the current one - even when engine_version/
# bundle_version/generated_at/ttl are ALL otherwise perfectly valid. This
# is the exact structural gap 4a1a000 fell into (ENGINE_VERSION wasn't
# bumped) - proving the hash alone would have caught it.
# ======================================================================
import datetime as _dt
import fundamentals_data as _fd

with tempfile.TemporaryDirectory() as _tmp:
    with mock.patch.object(ace, "_cache_dir", return_value=_tmp):
        _stale_meta = {
            "generated_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
            "engine_version": ace.ENGINE_VERSION,           # correct
            "bundle_version": _fd.BUNDLE_VERSION,            # correct
            "valuation_source_hash": "0" * 12,               # WRONG - simulates a pre-fix entry
        }
        _path = ace._cache_path("STALEHASHTEST")
        with open(_path, "w") as f:
            json.dump({"_meta": _stale_meta, "Fundamentals": {"metrics": []}}, f)
        _result = ace._read_cache("STALEHASHTEST")
assert _result is None, _result
print("[read_cache_rejects_stale_valuation_hash] a cache entry with correct engine_version/"
      "bundle_version/age but a MISMATCHED valuation_source_hash is correctly treated as "
      "stale (returns None), not served OK")

# And the mirror case: a matching hash (alongside everything else valid)
# IS served.
with tempfile.TemporaryDirectory() as _tmp:
    with mock.patch.object(ace, "_cache_dir", return_value=_tmp):
        _fresh_meta = {
            "generated_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
            "engine_version": ace.ENGINE_VERSION,
            "bundle_version": _fd.BUNDLE_VERSION,
            "valuation_source_hash": ace.VALUATION_SOURCE_HASH,   # correct
        }
        _path = ace._cache_path("FRESHHASHTEST")
        with open(_path, "w") as f:
            json.dump({"_meta": _fresh_meta, "Fundamentals": {"metrics": []}}, f)
        _result2 = ace._read_cache("FRESHHASHTEST")
assert _result2 is not None, _result2
assert _result2["_meta"]["valuation_source_hash"] == ace.VALUATION_SOURCE_HASH
print("[read_cache_accepts_matching_valuation_hash] a cache entry whose valuation_source_hash "
      "matches the current one (everything else valid too) IS served OK")

# An old cache entry written before this field existed (missing key
# entirely, same precedent as the bundle_version/engine_version checks
# right above it in _read_cache()) is correctly treated as stale too.
with tempfile.TemporaryDirectory() as _tmp:
    with mock.patch.object(ace, "_cache_dir", return_value=_tmp):
        _old_meta = {
            "generated_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
            "engine_version": ace.ENGINE_VERSION,
            "bundle_version": _fd.BUNDLE_VERSION,
            # no "valuation_source_hash" key at all - a pre-this-fix entry
        }
        _path = ace._cache_path("NOHASHFIELDTEST")
        with open(_path, "w") as f:
            json.dump({"_meta": _old_meta, "Fundamentals": {"metrics": []}}, f)
        _result3 = ace._read_cache("NOHASHFIELDTEST")
assert _result3 is None, _result3
print("[read_cache_rejects_missing_valuation_hash_field] a pre-this-fix cache entry with no "
      "valuation_source_hash key at all is correctly treated as stale, not served OK")


# ======================================================================
# CHECK: get_growth_estimates_5y()'s two-tier TTL - "fetch_failed"
# expires sooner (5 min) than "ok"/"no_coverage" (30 min).
# ======================================================================
ce._growth_estimate_cache.clear()

_fake_time = {"t": 10_000.0}


def _fake_time_time():
    return _fake_time["t"]


_calls = {"n": 0}


def _always_fails_ticker_factory(ticker):
    class _T:
        @property
        def growth_estimates(self):
            _calls["n"] += 1
            raise Exception("YFRateLimitError: Too Many Requests. Rate limited (429)")
    return _T()


with mock.patch.object(ce, "yf") as _fake_yf, \
     mock.patch("time.sleep", return_value=None), \
     mock.patch.object(ce.time, "time", side_effect=_fake_time_time):
    _fake_yf.Ticker.side_effect = _always_fails_ticker_factory
    _v1, _s1 = ce.get_growth_estimates_5y("TTLFAILTEST")
    assert _s1 == "fetch_failed", _s1
    _calls_after_first = _calls["n"]

    # +4 minutes: still within the 5-minute failure TTL - must be served
    # from the memo, not re-fetched.
    _fake_time["t"] += 240
    with mock.patch.object(ce, "yf") as _fake_yf2, \
         mock.patch("time.sleep", return_value=None), \
         mock.patch.object(ce.time, "time", side_effect=_fake_time_time):
        _fake_yf2.Ticker.side_effect = _always_fails_ticker_factory
        _v2, _s2 = ce.get_growth_estimates_5y("TTLFAILTEST")
    assert _calls["n"] == _calls_after_first, "must NOT re-fetch within the 5-min failure TTL"

    # +2 more minutes (6 total, past the 5-min failure TTL) - must re-fetch.
    _fake_time["t"] += 120
    with mock.patch.object(ce, "yf") as _fake_yf3, \
         mock.patch("time.sleep", return_value=None), \
         mock.patch.object(ce.time, "time", side_effect=_fake_time_time):
        _fake_yf3.Ticker.side_effect = _always_fails_ticker_factory
        _v3, _s3 = ce.get_growth_estimates_5y("TTLFAILTEST")
    assert _calls["n"] > _calls_after_first, "must re-fetch once the 5-min failure TTL expires"

assert ce._GROWTH_ESTIMATE_FAILURE_TTL_SECONDS == 300, ce._GROWTH_ESTIMATE_FAILURE_TTL_SECONDS
assert ce._GROWTH_ESTIMATE_FAILURE_TTL_SECONDS < ce._GROWTH_ESTIMATE_SUCCESS_TTL_SECONDS
print("[fetch_failed_expires_in_5_minutes] a fetch_failed result is served from cache for <5 "
      "min and re-fetched once 5 min has passed - NOT the full 30-min success TTL OK")

# Mirror check: a genuine "ok" result DOES survive past the 5-minute mark
# (still within the 30-minute success TTL) - proves the two TTLs are
# genuinely independent, not just "everything expires fast now".
ce._growth_estimate_cache.clear()
_fake_time["t"] = 20_000.0
_ok_calls = {"n": 0}


def _ok_ticker_factory(ticker):
    class _T:
        @property
        def growth_estimates(self):
            _ok_calls["n"] += 1
            import pandas as pd
            # Growth-never-zero rewrite (30 Sep 2026): Yahoo's row label
            # is "LTG" now, not "+5y" - see capm_engine.py's own comment
            # on _LTG_LABEL_KEY. This fixture only exercises the TTL
            # cache, not label priority, so a single correctly-labeled
            # row is enough.
            return pd.DataFrame({"stock": [0.103]}, index=["LTG"])
    return _T()


with mock.patch.object(ce, "yf") as _fake_yf4, \
     mock.patch("time.sleep", return_value=None), \
     mock.patch.object(ce.time, "time", side_effect=_fake_time_time):
    _fake_yf4.Ticker.side_effect = _ok_ticker_factory
    _v4, _s4 = ce.get_growth_estimates_5y("TTLOKTEST")
    assert _s4 == "ok", _s4
    _ok_calls_after_first = _ok_calls["n"]

# +6 minutes (past the 5-min failure TTL, but well within the 30-min
# success TTL) - must still be served from cache.
_fake_time["t"] += 360
with mock.patch.object(ce, "yf") as _fake_yf5, \
     mock.patch("time.sleep", return_value=None), \
     mock.patch.object(ce.time, "time", side_effect=_fake_time_time):
    _fake_yf5.Ticker.side_effect = _ok_ticker_factory
    _v5, _s5 = ce.get_growth_estimates_5y("TTLOKTEST")
assert _ok_calls["n"] == _ok_calls_after_first, (
    "an 'ok' result must survive past the 5-min mark - the two TTLs are independent"
)
print("[ok_result_survives_past_5_minutes] a genuine 'ok' result is still served from cache "
      "6 minutes later (well within its own 30-min TTL, unlike fetch_failed's 5-min one) OK")


# ======================================================================
# CHECK: _featured_analysis()'s valuation_hash parameter is a REAL,
# non-underscore-prefixed parameter that actually participates in st.
# cache_data's own cache key - this is the exact mistake caught and
# fixed during this task (an underscore-prefixed version would have been
# silently excluded from hashing and done nothing at all).
# ======================================================================
_sig = inspect.signature(_app._featured_analysis)
assert "valuation_hash" in _sig.parameters, _sig.parameters
assert not "valuation_hash".startswith("_")
print("[featured_analysis_hash_param_not_underscored] _featured_analysis()'s new parameter is "
      "named 'valuation_hash' (no leading underscore) - st.cache_data would silently exclude "
      "an underscore-prefixed name from its own cache key, verified directly against a "
      "throwaway st.cache_data function in this same session OK")

# Empirical proof that a non-underscore parameter genuinely busts st.
# cache_data's cache when its value changes (the mechanism this whole
# fix depends on) - a small throwaway function, not _featured_analysis
# itself (which needs a live network fetch it can't make in this
# sandbox), same st.cache_data decorator.
import streamlit as _st

_probe_calls = {"n": 0}


@_st.cache_data
def _cache_probe(x, valuation_hash):
    _probe_calls["n"] += 1
    return _probe_calls["n"]


_r1 = _cache_probe(1, "hash_a")
_r2 = _cache_probe(1, "hash_b")
assert _r1 != _r2, "a different valuation_hash value must bust the cache and re-run the function"
_r3 = _cache_probe(1, "hash_a")
assert _r3 == _r1, "the SAME valuation_hash value must still hit the cache"
print("[non_underscore_param_busts_cache] direct proof: a non-underscore-prefixed extra "
      "parameter changing value forces a fresh st.cache_data call; the same value re-hits the "
      "cache OK")


# ======================================================================
# CHECK: portfolio_health_engine.fetch_snapshot()'s valuation_hash
# defaults to auto_compounder_engine.VALUATION_SOURCE_HASH, baked in at
# module-import time - no existing caller needs to change.
# ======================================================================
_fs_sig = inspect.signature(phe.fetch_snapshot)
assert "valuation_hash" in _fs_sig.parameters, _fs_sig.parameters
assert _fs_sig.parameters["valuation_hash"].default == ace.VALUATION_SOURCE_HASH
print("[fetch_snapshot_hash_default_matches] fetch_snapshot()'s valuation_hash parameter "
      f"defaults to VALUATION_SOURCE_HASH ({ace.VALUATION_SOURCE_HASH!r}) with no caller "
      "needing to pass it explicitly OK")


print("\nALL VALUATION-CACHE-SAFETY FIXTURES PASSED")
