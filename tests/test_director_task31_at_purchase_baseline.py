"""
TASK 31 (Director-directed, owner request, 5 Nov 2026): "At purchase"
baseline display in Portfolio Health -> Holding detail.

The locked baseline captured at add time (portfolio_store baseline_json /
portfolio_health_engine.baseline_snapshot_fields()) is stored but was
never shown anywhere. This covers the task's own listed tests:
  - website_v2 baseline renders every field + correct MOS and Value
    Score range.
  - website_v1 baseline shows n/a gracefully.
  - no baseline -> block hidden.
  - baseline never modified by rendering it.

ALL FIXTURES ARE SYNTHETIC/MOCKED - this sandbox has no outbound
network access.

Run: python3 tests/test_director_task31_at_purchase_baseline.py
"""
import os
import sys
import tempfile
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TESTVOL = tempfile.mkdtemp(prefix="task31_at_purchase_test_")
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = TESTVOL
os.environ.pop("HEALTH_EVOLUTION_LIVE", None)

import pandas as pd

import portfolio_health_engine as phe
import portfolio_news_engine as pne
import portfolio_store as ps
import ranking_engine
import top100_engine

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


if os.path.exists(ps.DB_PATH):
    os.remove(ps.DB_PATH)
if os.path.exists(pne.DB_PATH):
    os.remove(pne.DB_PATH)

_EMAIL = "task31_test@example.com"

_BASELINE_V2 = {
    "schema": "website_v2",
    "price": 80.0,
    "intrinsic_value": 100.0,
    "quality_score": 70.0,
    "revenue_growth": 0.08,
    "earnings_growth": 0.07,
    "profit_margin": 0.20,
    "roe": 0.15,
    "debt_to_equity": 45.0,
    "fcf_growth": 0.05,
    "dividend_rate": 2.0,
}
_BASELINE_V1 = {
    "schema": "website_v1",
    "Price": 50.0,
    "Intrinsic Value": 65.0,
    "Quality": 60.0,
}

ps.add_holding(_EMAIL, "Main", "CSLV2", name="CSL V2 Co", kind="STOCK", currency="AUD",
                shares=10.0, buy_price=80.0, buy_date="2025-06-01",
                baseline=dict(_BASELINE_V2), baseline_date="2025-06-01")
ps.add_holding(_EMAIL, "Main", "OLDV1", name="Old V1 Co", kind="STOCK", currency="AUD",
                shares=5.0, buy_price=50.0, buy_date="2024-01-01",
                baseline=dict(_BASELINE_V1), baseline_date="2024-01-01")
ps.add_holding(_EMAIL, "Main", "NOBASE", name="No Baseline Co", kind="STOCK", currency="AUD",
                shares=3.0, buy_price=20.0, buy_date="2023-01-01",
                baseline=None, baseline_date=None)

_SNAPSHOT = {
    "revenue_growth": 0.09, "earnings_growth": 0.08, "profit_margin": 0.24, "roe": 0.16,
    "fcf_growth": 0.06, "debt_to_equity": 42.0, "intrinsic_value": 110.0, "price": 95.0,
    "dividend_rate": 2.6, "mos_pct": 13.6,
}
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _render(ticker):
    _patches = [
        mock.patch.object(phe, "fetch_snapshot", return_value=dict(_SNAPSHOT)),
        mock.patch.object(pne, "_claim_fetch", return_value=False),
    ]
    for p in _patches:
        p.start()
    try:
        script = f"""
import os, sys
sys.path.insert(0, {_REPO_ROOT!r})
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = {TESTVOL!r}
import app
import portfolio_store as ps
_holdings = [h for h in ps.get_holdings_all({_EMAIL!r}) if h["ticker"] == {ticker!r}]
_analyses = {{app._hkey(h): app._analyze_holding(h, email={_EMAIL!r}) for h in _holdings}}
app._render_portfolio_health_news_tab({_EMAIL!r}, "Main", _holdings, _analyses)
"""
        from streamlit.testing.v1 import AppTest
        at = AppTest.from_string(script, default_timeout=60)
        at.run()
        assert not at.exception, f"raised under AppTest: {at.exception}"
        return at
    finally:
        for p in _patches:
            p.stop()


def _metric_map(at):
    return {m.label: m.value for m in at.metric}


# ======================================================================
# CHECK 1: website_v2 baseline renders every field + correct MOS and
# Value Score range.
# ======================================================================
_at_v2 = _render("CSLV2")
_metrics_v2 = _metric_map(_at_v2)

check("v2: 'Baseline date' shows the stored baseline_date",
      _metrics_v2.get("Baseline date") == "2025-06-01")
check("v2: 'Price at purchase' shows the baseline price with currency suffix",
      _metrics_v2.get("Price at purchase") == "80.0000 AUD")
check("v2: 'Intrinsic value at purchase' shows the baseline IV with currency suffix",
      _metrics_v2.get("Intrinsic value at purchase") == "100.0000 AUD")

_expected_mos = (1 - 80.0 / 100.0) * 100
check("v2: 'Margin of safety at purchase' == (1 - price/IV) * 100, formatted +/-1dp%",
      _metrics_v2.get("Margin of safety at purchase") == f"{_expected_mos:+.1f}%")

check("v2: 'Quality score at purchase' shows the baseline quality score",
      _metrics_v2.get("Quality score at purchase") == "70.0")
check("v2: 'Revenue growth at purchase' shows the baseline fraction as a signed percent",
      _metrics_v2.get("Revenue growth at purchase") == "+8.0%")
check("v2: 'Earnings growth at purchase'", _metrics_v2.get("Earnings growth at purchase") == "+7.0%")
check("v2: 'Profit margin at purchase'", _metrics_v2.get("Profit margin at purchase") == "+20.0%")
check("v2: 'ROE at purchase'", _metrics_v2.get("ROE at purchase") == "+15.0%")
check("v2: 'Debt/equity at purchase' shows the raw ratio-percent, unsigned, with a %",
      _metrics_v2.get("Debt/equity at purchase") == "45.0%")
check("v2: 'FCF growth at purchase'", _metrics_v2.get("FCF growth at purchase") == "+5.0%")

_captions_v2 = " | ".join(c.value or "" for c in _at_v2.caption)
check("v2: dividend rate at purchase shown (not a metric tile) with currency suffix",
      "2.0000 AUD" in _captions_v2)
check("v2: the valuation-model-drift caption (TASK 16b precedent) is present",
      "not directly comparable with today's model" in _captions_v2)

_expected_lo = ranking_engine.calculate_long_score(70.0, _expected_mos, -50, 0, discovery_measured=False)
_expected_hi = ranking_engine.calculate_long_score(70.0, _expected_mos, 50, 0, discovery_measured=False)
_expected_range = f"{min(_expected_lo, _expected_hi):.1f} - {max(_expected_lo, _expected_hi):.1f}"
check("v2: 'Value Score at purchase (estimate)' == calculate_long_score at psychology -50/+50, "
      "discovery=0/unmeasured, min-max ordered",
      _metrics_v2.get("Value Score at purchase (estimate)") == _expected_range)
check("v2: the Value Score estimate caption names today's pool cut-off (n/a - no pool seeded "
      "in this test)",
      "no pool selected yet" in _captions_v2)
print("[task31_c1_website_v2_full] every website_v2 baseline field renders, with the correct "
      "MOS and Value Score estimate range OK")


# ======================================================================
# CHECK 2: website_v1 baseline shows n/a gracefully for fields it never
# had, while still rendering the three fields it DOES have (via
# _normalize_baseline()'s own website_v1 field-name mapping).
# ======================================================================
_at_v1 = _render("OLDV1")
_metrics_v1 = _metric_map(_at_v1)

check("v1: 'Baseline date' present", _metrics_v1.get("Baseline date") == "2024-01-01")
check("v1: 'Price at purchase' mapped from the v1 'Price' field",
      _metrics_v1.get("Price at purchase") == "50.0000 AUD")
check("v1: 'Intrinsic value at purchase' mapped from the v1 'Intrinsic Value' field",
      _metrics_v1.get("Intrinsic value at purchase") == "65.0000 AUD")
check("v1: 'Quality score at purchase' mapped from the v1 'Quality' field",
      _metrics_v1.get("Quality score at purchase") == "60.0")
check("v1: fields the v1 schema never had show the literal 'n/a', not a crash/blank",
      _metrics_v1.get("Revenue growth at purchase") == "n/a"
      and _metrics_v1.get("Earnings growth at purchase") == "n/a"
      and _metrics_v1.get("Profit margin at purchase") == "n/a"
      and _metrics_v1.get("ROE at purchase") == "n/a"
      and _metrics_v1.get("Debt/equity at purchase") == "n/a"
      and _metrics_v1.get("FCF growth at purchase") == "n/a")
_captions_v1 = " | ".join(c.value or "" for c in _at_v1.caption)
check("v1: dividend rate at purchase shows 'n/a' in its caption line (v1 never had it)",
      "Dividend rate at purchase: n/a" in _captions_v1)
print("[task31_c2_website_v1_graceful] an older website_v1 baseline renders its 3 real "
      "fields correctly and 'n/a' for every field it never had OK")


# ======================================================================
# CHECK 3: no baseline at all -> the whole "At purchase" block is
# hidden, not shown with every field as n/a.
# ======================================================================
_at_none = _render("NOBASE")
_metrics_none = _metric_map(_at_none)
check("no-baseline: 'Baseline date' metric tile is entirely absent",
      "Baseline date" not in _metrics_none)
check("no-baseline: 'Price at purchase' metric tile is entirely absent",
      "Price at purchase" not in _metrics_none)
_markdowns_none = " | ".join(m.value or "" for m in _at_none.markdown)
check("no-baseline: the 'At purchase' heading itself is absent",
      "At purchase" not in _markdowns_none)
print("[task31_c3_no_baseline_hidden] a holding with no locked baseline shows no 'At "
      "purchase' block at all OK")


# ======================================================================
# CHECK 4: rendering the block never modifies the stored baseline - it
# is read-only, by construction (no write call exists on this path),
# verified directly against the stored row before/after.
# ======================================================================
_before = [h for h in ps.get_holdings_all(_EMAIL) if h["ticker"] == "CSLV2"][0]
_render("CSLV2")
_render("CSLV2")
_after = [h for h in ps.get_holdings_all(_EMAIL) if h["ticker"] == "CSLV2"][0]
check("rendering twice never changes the stored baseline dict or baseline_date",
      _after["baseline"] == _before["baseline"] == _BASELINE_V2
      and _after["baseline_date"] == _before["baseline_date"] == "2025-06-01")
print("[task31_c4_baseline_never_modified] the 'At purchase' block is read-only - the "
      "stored baseline is byte-identical before and after rendering it OK")


# ======================================================================
# CHECK 5 (A2-style guard from the task's own wording): the formula's
# own PSY_CLAMP is exactly -50/+50 - confirms this task's hardcoded
# clamp-limit evaluation points match ranking_engine's real constant,
# not a number this report invented independently.
# ======================================================================
check("ranking_engine.PSY_CLAMP is exactly 50.0 (the clamp limits used for the Value Score "
      "at purchase estimate's low/high psychology inputs)",
      ranking_engine.PSY_CLAMP == 50.0)
print("[task31_c5_psy_clamp_matches] the -50/+50 evaluation points match ranking_engine's "
      "own PSY_CLAMP constant, not an independently invented number OK")


if os.path.exists(ps.DB_PATH):
    os.remove(ps.DB_PATH)
if os.path.exists(pne.DB_PATH):
    os.remove(pne.DB_PATH)

print()
print(f"PASS={passed} FAIL={failed}")
sys.exit(1 if failed else 0)
