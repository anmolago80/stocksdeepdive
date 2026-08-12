"""
backport_ticker_fix.py

One-off maintenance utility: copy ONE ticker's Company Potential content
from the CURRENT (corrected) compounder_data.json into every ARCHIVED
snapshot, in place.

WHY THIS EXISTS: archived snapshots are meant to be an honest historical
record - but the first archives captured a data-entry error (Nubank notes
pasted into AUB's Company Potential row), which is corrupted data, not
genuine history. This backports the corrected text into the archives so
history shows what the research actually said, without touching any other
ticker, section, number or timestamp in the archived files.

USAGE (from the Railway service Console, or locally in the site folder):

    python backport_ticker_fix.py AUB.AX

Add --all-sections to backport EVERY section's data for that ticker (not
just Company Potential) - only needed if numbers were wrong too:

    python backport_ticker_fix.py AUB.AX --all-sections

The script prints exactly what it changed in each archive. Safe to re-run
(idempotent). It never touches the live compounder_data.json.
"""

import json
import os
import sys

import build_compounder_data as bcd

CP_SECTION = "Company Potential"
CP_PER_TICKER_KEYS = ("hml_ratings", "yesno_checks", "text_groups")


def _load(path):
    with open(path) as f:
        return json.load(f)


def _save(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, path)


def _backport_company_potential(archive, current, ticker):
    """Replace `ticker`'s Company Potential entries in `archive` with the
    current dataset's version (or remove them if the current dataset has
    none). Returns a list of human-readable change notes."""
    changes = []
    arc_cp = archive.get("sections", {}).get(CP_SECTION)
    cur_cp = current.get("sections", {}).get(CP_SECTION, {})
    if not arc_cp:
        return changes
    for key in CP_PER_TICKER_KEYS:
        arc_map = arc_cp.get(key)
        if not isinstance(arc_map, dict) or ticker not in arc_map:
            continue
        cur_val = cur_cp.get(key, {}).get(ticker)
        if cur_val is not None:
            if arc_map[ticker] != cur_val:
                arc_map[ticker] = cur_val
                changes.append(f"{CP_SECTION}/{key}: replaced with current")
        else:
            del arc_map[ticker]
            changes.append(f"{CP_SECTION}/{key}: removed (no current data)")
    return changes


def _backport_all_sections(archive, current, ticker):
    """Replace `ticker`'s per-ticker values in EVERY section (metric
    values and per-ticker sub-maps) with the current dataset's version."""
    changes = []
    for sec_name, arc_sec in archive.get("sections", {}).items():
        cur_sec = current.get("sections", {}).get(sec_name, {})
        # metric values: sections carry metrics = [{key, values: {tkr: v}}]
        cur_metric_vals = {
            m.get("key"): m.get("values", {}).get(ticker)
            for m in cur_sec.get("metrics", [])
        }
        for m in arc_sec.get("metrics", []):
            if ticker in m.get("values", {}):
                cur_v = cur_metric_vals.get(m.get("key"))
                if cur_v is not None and m["values"][ticker] != cur_v:
                    m["values"][ticker] = cur_v
                    changes.append(f"{sec_name}/metric {m.get('key')}: updated")
        # per-ticker sub-maps (price_history, series, valuation_methods, ...)
        for key, arc_val in list(arc_sec.items()):
            if key == "metrics" or not isinstance(arc_val, dict):
                continue
            if ticker in arc_val:
                cur_val = cur_sec.get(key, {}).get(ticker) if isinstance(cur_sec.get(key), dict) else None
                if cur_val is not None and arc_val[ticker] != cur_val:
                    arc_val[ticker] = cur_val
                    changes.append(f"{sec_name}/{key}: updated")
    return changes


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    all_sections = "--all-sections" in sys.argv
    if not args:
        print("Usage: python backport_ticker_fix.py <TICKER> [--all-sections]")
        sys.exit(1)
    ticker = args[0].strip().upper()

    current_path = os.path.join(bcd._cp_data_dir(), "compounder_data.json")
    if not os.path.exists(current_path):
        print(f"No current dataset found at {current_path} - nothing to backport from.")
        sys.exit(1)
    current = _load(current_path)
    if ticker not in current.get("tickers", {}):
        print(f"WARNING: {ticker} is not in the CURRENT dataset - archived "
              "entries for it will be REMOVED rather than replaced.")

    snapshots = bcd.list_archived_snapshots()
    if not snapshots:
        print("No archived snapshots found - nothing to do.")
        return

    total = 0
    for snap in snapshots:
        archive = _load(snap["path"])
        changes = _backport_company_potential(archive, current, ticker)
        if all_sections:
            changes += _backport_all_sections(archive, current, ticker)
        if changes:
            _save(snap["path"], archive)
            total += 1
            print(f"[fixed] {os.path.basename(snap['path'])} ({snap['label']}):")
            for c in changes:
                print(f"    - {c}")
        else:
            print(f"[clean] {os.path.basename(snap['path'])} ({snap['label']}): no changes needed")
    print(f"Done - {total} archive(s) updated for {ticker}.")


if __name__ == "__main__":
    main()
