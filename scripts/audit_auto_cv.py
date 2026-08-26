"""
scripts/audit_auto_cv.py

One-off audit harness for the auto Compounder View engine
(auto_compounder_engine.py). For each ticker passed on the command line,
recomputes the engine's live sections (force_refresh=True, so nothing
stale from an earlier run of the app can hide behind the cache) and, for
any ticker that also has hand-built reference data in compounder_data.json
(AUB.AX, CSL.AX, RMD.AX as of this writing), prints a side-by-side
auto-vs-workbook comparison table per section with a %deviation column,
flagging anything over 40% with "<<< CHECK".

For a ticker with NO workbook row (default: OCL.AX) there's nothing to
diff against, so instead this prints a diagnostic dump of the raw inputs
and every Fair Value method's output, and sanity-checks that each method
lands within a 0.2x-5x band around the current price - a valuation method
that isn't at least in the same neighbourhood as the market price is
almost certainly a formula bug, not a genuinely contrarian view.

Usage:
    python scripts/audit_auto_cv.py                          # default set
    python scripts/audit_auto_cv.py AUB.AX RMD.AX             # subset
    python scripts/audit_auto_cv.py OCL.AX                    # diagnostic-only

This script makes real network calls (via auto_compounder_engine ->
fundamentals_data -> yfinance/EODHD) and is meant to be run somewhere with
live network access - it is not part of the app's request path and is not
imported by app.py.
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import auto_compounder_engine  # noqa: E402

DEFAULT_TICKERS = ["AUB.AX", "RMD.AX", "CSL.AX", "OCL.AX"]
DEVIATION_FLAG_PCT = 40.0
SANITY_LOW, SANITY_HIGH = 0.2, 5.0


def _load_workbook():
    path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "compounder_data.json",
    )
    try:
        with open(path) as f:
            return json.load(f)
    except Exception as e:
        print(f"!! Couldn't load compounder_data.json: {e}")
        return None


def _pct_dev(auto, workbook):
    if auto is None or workbook is None:
        return None
    if not isinstance(auto, (int, float)) or not isinstance(workbook, (int, float)):
        return None
    if workbook == 0:
        return None
    return (auto - workbook) / abs(workbook) * 100.0


def _fmt(v):
    if v is None:
        return "-"
    if isinstance(v, (int, float)):
        return f"{v:,.4f}"
    return str(v)


def _print_row(label, auto_v, wb_v):
    dev = _pct_dev(auto_v, wb_v)
    dev_str = f"{dev:+.1f}%" if dev is not None else "-"
    flag = " <<< CHECK" if (dev is not None and abs(dev) > DEVIATION_FLAG_PCT) else ""
    print(f"  {label:<38} {_fmt(auto_v):>16} {_fmt(wb_v):>16} {dev_str:>10}{flag}")


def _compare_metrics(section_name, auto_section, wb_section, ticker):
    auto_metrics = {m["label"]: m["values"].get(ticker) for m in (auto_section.get("metrics") or [])}
    wb_metrics = {m["label"]: m["values"].get(ticker) for m in (wb_section.get("metrics") or [])}
    labels = [m["label"] for m in (wb_section.get("metrics") or [])]
    for label in labels:
        if label not in auto_metrics:
            continue
        _print_row(label, auto_metrics.get(label), wb_metrics.get(label))


def _compare_valuation_methods(auto_section, wb_section, ticker):
    auto_vm = (auto_section.get("valuation_methods") or {}).get(ticker, {})
    wb_vm = (wb_section.get("valuation_methods") or {}).get(ticker, {})
    for key in wb_vm:
        if key in auto_vm:
            _print_row(f"valuation_methods.{key}", auto_vm.get(key), wb_vm.get(key))


def _compare_value_created(auto_section, wb_section, ticker):
    auto_vc = (auto_section.get("value_created") or {}).get(ticker, {})
    wb_vc = (wb_section.get("value_created") or {}).get(ticker, {})
    for horizon in wb_vc:
        auto_h = auto_vc.get(horizon, {})
        wb_h = wb_vc.get(horizon, {})
        for field in ("retained_earnings", "value_created"):
            _print_row(f"value_created.{horizon}.{field}", auto_h.get(field), wb_h.get(field))


def _compare_wacc_roic(auto_section, wb_section, ticker):
    auto_s = (auto_section.get("wacc_roic_series") or {}).get(ticker, {})
    wb_s = (wb_section.get("wacc_roic_series") or {}).get(ticker, {})
    for series_name in ("wacc", "roic"):
        wb_periods = (wb_s.get(series_name) or {}).get("periods", [])
        wb_values = (wb_s.get(series_name) or {}).get("values", [])
        auto_periods = (auto_s.get(series_name) or {}).get("periods", [])
        auto_values = (auto_s.get(series_name) or {}).get("values", [])
        auto_by_period = dict(zip(auto_periods, auto_values))
        wb_by_period = dict(zip(wb_periods, wb_values))
        for period in wb_periods:
            _print_row(f"{series_name}[{period}]", auto_by_period.get(period), wb_by_period.get(period))


def audit_against_workbook(ticker, auto_sections, workbook):
    wb_sections = workbook.get("sections", {})
    print(f"\n{'=' * 90}\n{ticker} - auto vs workbook\n{'=' * 90}")

    for section_name in ["Fundamentals", "Value vs Book", "Retained Earnings",
                          "Earnings Trends", "Cost of Capital", "Fair Value"]:
        auto_section = auto_sections.get(section_name) or {}
        wb_section = wb_sections.get(section_name) or {}
        if not wb_section:
            continue
        print(f"\n-- {section_name} --")
        print(f"  {'metric':<38} {'auto':>16} {'workbook':>16} {'%dev':>10}")
        _compare_metrics(section_name, auto_section, wb_section, ticker)
        if section_name == "Fair Value":
            _compare_valuation_methods(auto_section, wb_section, ticker)
        if section_name == "Retained Earnings":
            _compare_value_created(auto_section, wb_section, ticker)
        if section_name == "Cost of Capital":
            _compare_wacc_roic(auto_section, wb_section, ticker)


def diagnostic_dump(ticker, auto_sections):
    print(f"\n{'=' * 90}\n{ticker} - no workbook row, diagnostic dump + sanity check\n{'=' * 90}")

    fv = auto_sections.get("Fair Value") or {}
    re_sec = auto_sections.get("Retained Earnings") or {}

    re_metrics = {m["label"]: m["values"].get(ticker) for m in (re_sec.get("metrics") or [])}
    print("\n-- Retained Earnings inputs --")
    for label in ("EPS (TTM)", "Dividend (TTM)", "Retained Earnings (TTM)",
                  "10Y Retained Earnings (From Last FY)"):
        print(f"  {label:<40} {_fmt(re_metrics.get(label))}")

    vm = (fv.get("valuation_methods") or {}).get(ticker, {})
    vi = (fv.get("valuation_inputs") or {}).get(ticker, {})
    price = vm.get("price")
    print(f"\n-- Fair Value methods (price = {_fmt(price)}) --")
    any_check = False
    for method, value in vm.items():
        if method == "price":
            continue
        inputs = vi.get(method) or []
        inputs_str = ", ".join(f"{i['label']}={_fmt(i['value'])}" for i in inputs)
        ratio = (value / price) if (isinstance(value, (int, float)) and price) else None
        sane = ratio is not None and SANITY_LOW <= ratio <= SANITY_HIGH
        flag = "" if sane else " <<< CHECK"
        ratio_str = f"{ratio:.2f}x price" if ratio is not None else "n/a"
        print(f"  {method:<16} = {_fmt(value):>14}  ({ratio_str}){flag}")
        if inputs_str:
            print(f"      inputs: {inputs_str}")
        if not sane:
            any_check = True
    if not vm:
        print("  (no valuation methods computed - engine likely returned None for all of them)")
        any_check = True
    print(f"\n  Sanity check ({SANITY_LOW}x-{SANITY_HIGH}x price band): "
          f"{'FAIL - see <<< CHECK above' if any_check else 'PASS'}")


def main():
    tickers = sys.argv[1:] or DEFAULT_TICKERS
    workbook = _load_workbook()
    wb_tickers = set((workbook or {}).get("sections", {}).get("Fair Value", {})
                      .get("valuation_methods", {}).keys())

    for ticker in tickers:
        print(f"\nComputing live sections for {ticker} (force_refresh=True)...")
        try:
            sections = auto_compounder_engine.build_sections(ticker, force_refresh=True)
        except Exception as e:
            print(f"!! build_sections raised for {ticker}: {e!r}")
            continue
        if not sections:
            print(f"!! build_sections returned nothing for {ticker} - "
                  "no statement/price data available.")
            continue

        if ticker in wb_tickers and workbook:
            audit_against_workbook(ticker, sections, workbook)
        else:
            diagnostic_dump(ticker, sections)


if __name__ == "__main__":
    main()
