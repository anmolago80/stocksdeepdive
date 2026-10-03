"""
Lists & display, Commit 5 - currency symbols and decimals (Director-
directed, 3 Oct 2026).

ALL FIXTURES IN THIS FILE ARE SYNTHETIC - this sandbox has no outbound
network access, same disclosure as every other fixture-based test in
this repo.

Covers:
  - currency_format.symbol_for_ticker()/format_money(): USD/AUD
    unchanged (default_symbol passed through), GBP -> £, CAD -> C$,
    JPY -> ¥ with 0 decimals + thousands separators.
  - Every changed site, one fixture per currency, USD/AUD byte-
    identical to its pre-Commit-5 behaviour:
      compounder_ui._cp_format(fmt="cur", ticker=...)
      research_snapshot_render._fmt(fmt="cur", ticker=...)
      og_card_render._fmt_money(ticker=...)
      results_engine.fmt_metric()/_delta_text(ticker=...)
  - Full regression suite green (run separately, not in this file).

Run: python3 tests/test_lists_display_commit5_currency_format.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import currency_format as cf
import compounder_ui
import research_snapshot_render as rsr
import og_card_render as ogr
import results_engine as re_


# ======================================================================
# T1: currency_format core - USD/AUD unchanged, GBP/CAD/JPY own symbol.
# Values kept under 1000 so the magnitude rule's default is 2dp - see
# the separate T1_magnitude_rule block below for the >=1000 case.
# ======================================================================
assert cf.format_money(234.5, "AAPL") == "$234.50"
assert cf.format_money(234.5, "CSL.AX") == "$234.50"
assert cf.format_money(234.5, "TSCO.L") == "£234.50"
assert cf.format_money(234.5, "RY.TO") == "C$234.50"
assert cf.format_money(234.5, "7203.T") == "¥234"  # JPY: always 0dp, rounded
assert cf.format_money(None, "TSCO.L") is None
print("[T1_core] USD/AUD unchanged, GBP=£, CAD=C$, JPY=¥ with 0dp OK")

# magnitude rule preserved when decimals=None (0dp >=1000, else 2dp)
assert cf.format_money(1234.5, "AAPL") == "$1,234"
assert cf.format_money(12.5, "AAPL") == "$12.50"
print("[T1_magnitude_rule] USD/AUD magnitude-based decimals rule unchanged OK")


# ======================================================================
# T2: compounder_ui._cp_format() - omitted ticker byte-identical;
# ticker threaded in for GBP/CAD/JPY.
# ======================================================================
assert compounder_ui._cp_format(234.5, "cur") == "$234.50"
assert compounder_ui._cp_format(234.5, "cur", ticker="AAPL") == "$234.50"
assert compounder_ui._cp_format(234.5, "cur", ticker="CSL.AX") == "$234.50"
assert compounder_ui._cp_format(234.5, "cur", ticker="TSCO.L") == "£234.50"
assert compounder_ui._cp_format(234.5, "cur", ticker="RY.TO") == "C$234.50"
assert compounder_ui._cp_format(234.5, "cur", ticker="7203.T") == "¥234"
assert compounder_ui._cp_format(1234.5, "pct") == "123,450.0%"  # non-cur fmt untouched
print("[T2_compounder_ui] _cp_format(ticker=...) per currency, omitted-ticker "
      "byte-identical OK")


# ======================================================================
# T3: research_snapshot_render._fmt() - same contract.
# ======================================================================
assert rsr._fmt(234.5, "cur") == "$234.50"
assert rsr._fmt(234.5, "cur", ticker="AAPL") == "$234.50"
assert rsr._fmt(234.5, "cur", ticker="TSCO.L") == "£234.50"
assert rsr._fmt(234.5, "cur", ticker="RY.TO") == "C$234.50"
assert rsr._fmt(234.5, "cur", ticker="7203.T") == "¥234"
print("[T3_research_snapshot_render] _fmt(ticker=...) per currency, omitted-ticker "
      "byte-identical OK")


# ======================================================================
# T4: og_card_render._fmt_money() - same contract.
# ======================================================================
assert ogr._fmt_money(234.5) == "$234.50"
assert ogr._fmt_money(234.5, ticker="AAPL") == "$234.50"
assert ogr._fmt_money(234.5, ticker="CSL.AX") == "$234.50"
assert ogr._fmt_money(234.5, ticker="TSCO.L") == "£234.50"
assert ogr._fmt_money(234.5, ticker="RY.TO") == "C$234.50"
assert ogr._fmt_money(234.5, ticker="7203.T") == "¥234"
assert ogr._fmt_money(None) == "–"
assert ogr._fmt_money("n/a") == "–"
print("[T4_og_card_render] _fmt_money(ticker=...) per currency, omitted-ticker "
      "byte-identical, None/non-numeric -> em-dash OK")


# ======================================================================
# T5: results_engine.fmt_metric()/_delta_text() - same contract, plus
# money_compact (fcf_base) deliberately untouched by this commit (its
# own pre-existing fcf_currency text-suffix convention is the only
# currency label for that metric - see fmt_metric()'s own docstring).
# ======================================================================
_meta_money = re_.METRIC_META["intrinsic_value"]
assert re_.fmt_metric(_meta_money, 234.5) == "$234.50"
assert re_.fmt_metric(_meta_money, 234.5, ticker="AAPL") == "$234.50"
assert re_.fmt_metric(_meta_money, 234.5, ticker="TSCO.L") == "£234.50"
assert re_.fmt_metric(_meta_money, 234.5, ticker="RY.TO") == "C$234.50"
assert re_.fmt_metric(_meta_money, 234.5, ticker="7203.T") == "¥234"

_meta_pts = re_.METRIC_META["quality"]
assert re_.fmt_metric(_meta_pts, 55.4, ticker="TSCO.L") == "55"  # non-money kind untouched

assert re_._delta_text("money", 12.5) == "+$12.50"
assert re_._delta_text("money", 12.5, ticker="TSCO.L") == "+£12.50"
assert re_._delta_text("money", -12.5, ticker="RY.TO") == "C$-12.50"
assert re_._delta_text("money_compact", 1_500_000) == "+$1.50M"  # untouched by this commit
print("[T5_results_engine] fmt_metric()/_delta_text(ticker=...) per currency, "
      "non-money kinds and money_compact (fcf_base) untouched OK")

_before = {"intrinsic_value": 100.0, "quality": 50.0}
_after = {"intrinsic_value": 110.0, "quality": 55.0}
_moved_no_ticker = re_.what_moved(_before, _after)
_moved_gbp = re_.what_moved(_before, _after, ticker="TSCO.L")
assert any("$100.00" in m["text"] for m in _moved_no_ticker), _moved_no_ticker
assert any("£100.00" in m["text"] for m in _moved_gbp), _moved_gbp
print("[T5_what_moved] what_moved(ticker=...) bullet text uses the ticker's own "
      "currency symbol; omitted-ticker byte-identical OK")


print("\nALL Lists & display Commit 5 (currency symbols and decimals) CHECKS PASSED")
