"""
verify_moat_acceptance.py - Part A acceptance spot-check (throwaway, not a
permanent site file - matches the verify_*.py.bak convention already used
in this repo for one-off engine checks).

Computes moat_engine.compute_moat() for the five acceptance tickers and
prints a plain-text report. Requires real internet access to Yahoo Finance
(and, for full 10-year depth, EODHD_API_KEY) - run this from an ordinary
local terminal with normal internet access, NOT from a sandboxed/proxied
shell (see the run notes this script's caller left in the final summary
for why it couldn't be run from either Claude sandbox this session had
available).

Usage:  python verify_moat_acceptance.py
"""

import moat_engine

TICKERS = ["CSL.AX", "OCL.AX", "AUB.AX", "RMD.AX", "IVV.AX"]

for t in TICKERS:
    print("=" * 70)
    print(t)
    print("=" * 70)
    result = moat_engine.compute_moat(t, force_refresh=True)
    print(f"  score:   {result['score']}")
    print(f"  mode:    {result['mode']}")
    print(f"  erosion: {result['erosion']}")
    print(f"  years:   {result['years']}")
    if result["components"]:
        print("  components:")
        for c in result["components"]:
            print(f"    {c['pillar']:<24} {c['points']:>6} / {c['max']}")
    else:
        print("  components: (none - see flags)")
    if result["flags"]:
        print("  flags:")
        for f in result["flags"]:
            print(f"    - {f}")
    print()
