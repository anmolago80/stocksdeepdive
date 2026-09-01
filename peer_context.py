"""
peer_context.py

Services batch 2, Part 2 (2026-09-01): "Peer context on the Deep Dive" -
percentile ranks and closest peers for a ticker, computed ENTIRELY from
already-saved overnight scan tables (scan_store.load_scan) - no live
network calls of any kind, so this is cheap enough to compute on every
Deep Dive page view (app.py additionally wraps the call in a short-TTL
st.cache_data, since a repeat view of the same ticker within a few
minutes shouldn't re-read every scan file from disk again).

Both the ticker's own numbers and every peer's numbers come from the SAME
overnight scan row shape (nightly_scan.analyze_ticker_lite()'s own field
names - "Long Score", "Quality", "Moat", "MOS %", "Psychology", "Sector"),
so a percentile is always comparing like with like - never a live,
just-computed Deep Dive figure against a stale overnight one for
everyone else. "Sector" is a Part 2 addition to those rows (see
nightly_scan.py's own comment on _attach_moat/run_universe_scan) - a scan
saved before this shipped simply has no Sector on any row yet, which this
module treats as "sector context not available", never a crash (see
compute()'s own "if sector" branches).

A ticker that isn't a member of any loaded universe's LAST scan simply
has no peer context yet - see compute()'s own "not_scanned" case. Two
different real-world situations collapse into that one case by
construction, not by a special check: an ETF/fund (the equity-index
constituent lists this site scans never include funds in the first
place, so one is never a scan row to begin with) and a stock whose home
universe genuinely hasn't been scanned yet. app.py tells these apart at
the UI layer BEFORE ever calling this module for a fund (hiding the
block entirely, via the same moat_engine._is_fund() check the Moat Score
itself already relies on) - so in practice this module's own
"not_scanned" case is reached only by the second, genuine situation.
"""

import fundamentals_data
import scan_store
import scanner_engine

# Preferred universe search order per market - "the ticker's home
# universe" per the spec, tried in order until one is found whose LAST
# saved scan actually contains this ticker.
_AU_PRIORITY = ["ASX 300", "ASX 200", "All Ordinaries", "ASX Small Ordinaries"]
_US_PRIORITY = ["S&P 500", "S&P 1500", "Nasdaq 100", "Dow Jones 30",
                "Russell 1000", "S&P 400 MidCap", "Small Caps (S&P 600)",
                "Russell 2000"]

# (scan row field, public percentile key) - all five are "higher = better"
# in this app's own convention (Psychology included: fear enters with a
# positive sign, see site_content.py's methodology text), so none of them
# needs inverting before ranking.
_METRICS = [
    ("Long Score", "value_score"),
    ("Quality", "quality"),
    ("Moat", "moat"),
    ("MOS %", "mos"),
    ("Psychology", "psychology"),
]

PEER_TABLE_SIZE = 5


def _percentile(value, population):
    """0-100, higher = better - the standard percentile-rank formula,
    ties split down the middle: (count strictly below + 0.5 * count
    equal) / total * 100. `population` is expected to include the row
    `value` itself came from (same as every other row) rather than being
    pre-filtered to exclude it - its self-match is exactly one of the
    "equal" ties, which the 0.5 weighting already treats correctly, so
    there's no separate self-exclusion step needed. None values in
    `population` are dropped before ranking; returns None if `value`
    itself is None or nothing usable remains to rank against."""
    others = [v for v in population if v is not None]
    if value is None or not others:
        return None
    below = sum(1 for v in others if v < value)
    equal = sum(1 for v in others if v == value)
    return round(((below + 0.5 * equal) / len(others)) * 100, 1)


def _find_in_universe(ticker, universe):
    """(payload, row) for `ticker` inside `universe`'s last saved scan, or
    (None, None) if that universe has no scan, or the scan doesn't
    contain this ticker."""
    payload = scan_store.load_scan(universe)
    if not payload:
        return None, None
    for row in payload["rows"]:
        if (row.get("Ticker") or "").upper() == ticker:
            return payload, row
    return None, None


def _locate(ticker, priority, all_universes):
    """(universe_name, payload, row) - tries `priority` in order first
    (the ticker's likely "home" universe), then falls back to checking
    every OTHER universe scanner_engine knows about for this market and
    taking the LARGEST (by row count) whose last scan actually contains
    the ticker, per the spec's "else the largest one containing it".
    (None, None, None) if nothing has this ticker at all."""
    for u in priority:
        payload, row = _find_in_universe(ticker, u)
        if row is not None:
            return u, payload, row

    best = (None, None, None)
    best_size = -1
    for u in all_universes:
        if u in priority:
            continue
        payload, row = _find_in_universe(ticker, u)
        if row is not None and len(payload["rows"]) > best_size:
            best = (u, payload, row)
            best_size = len(payload["rows"])
    return best


def _closest_peers(ticker, own_row, rows, sector):
    """Up to PEER_TABLE_SIZE closest peers: same sector, nearest by market
    cap - a per-candidate PEEK at whatever fundamentals bundle is already
    cached for it (fundamentals_data.peek_cached_market_cap() - no
    network call, ever). Falls back to nearest-by-Quality when this
    ticker's OWN market cap isn't cached (nothing to measure distance
    from). A candidate peer with no cached cap of its own (in the
    market-cap branch) sorts to the back rather than being dropped, so a
    thin fundamentals cache never shrinks the peer list below what
    sector membership alone would give."""
    if not sector:
        return []
    candidates = [
        r for r in rows
        if r.get("Sector") == sector and (r.get("Ticker") or "").upper() != ticker
    ]
    if not candidates:
        return []

    own_cap = fundamentals_data.peek_cached_market_cap(ticker)
    if own_cap:
        def _key(r):
            cap = fundamentals_data.peek_cached_market_cap(r.get("Ticker"))
            return (1, 0.0) if cap is None else (0, abs(cap - own_cap))
        candidates.sort(key=_key)
    else:
        own_quality = own_row.get("Quality")
        def _key(r):
            q = r.get("Quality")
            return (1, 0.0) if (q is None or own_quality is None) else (0, abs(q - own_quality))
        candidates.sort(key=_key)

    peers = []
    for r in candidates[:PEER_TABLE_SIZE]:
        peers.append({
            "ticker": r.get("Ticker"),
            "price": r.get("Price"),
            "intrinsic_value": r.get("Intrinsic Value"),
            "mos": r.get("MOS %"),
            "value_score": r.get("Long Score"),
            "quality": r.get("Quality"),
            "moat": r.get("Moat"),
        })
    return peers


def attach_percentiles(rows):
    """Batch equivalent of compute()'s own per-metric percentile step,
    called ONCE per nightly universe scan (nightly_scan.run_universe_scan,
    right before scan_store.save_scan) rather than per Deep Dive view -
    every row in a freshly-scanned universe needs percentiles against the
    SAME population, so it's computed once here and stored on each row as
    row["Percentiles"], in the same {"universe": .., "sector": ..} shape
    per metric that compute() returns. This is what actually reaches
    snapshot_store/the public API/MCP (see snapshot_store._PUBLIC_FIELD_MAP's
    "Percentiles" entry) - compute() (used live by the Deep Dive page)
    recomputes independently from the same saved rows, so the two are
    always numerically identical, never a separate, driftable calculation.

    Mutates every dict in `rows` in place (adds "Percentiles"); also
    returns `rows` for convenience. Rows with no Sector get
    {"universe": .., "sector": None} for every metric, same convention
    compute() uses."""
    universe_values = {field: [r.get(field) for r in rows] for field, _ in _METRICS}

    sector_groups = {}
    for row in rows:
        sec = row.get("Sector")
        if sec:
            sector_groups.setdefault(sec, []).append(row)
    sector_values = {
        sec: {field: [r.get(field) for r in group] for field, _ in _METRICS}
        for sec, group in sector_groups.items()
    }

    for row in rows:
        sector = row.get("Sector")
        row["Percentiles"] = {
            key: {
                "universe": _percentile(row.get(field), universe_values[field]),
                "sector": (_percentile(row.get(field), sector_values[sector][field])
                           if sector else None),
            }
            for field, key in _METRICS
        }
    return rows


def compute(ticker):
    """
    Percentile context + closest peers for `ticker`, purely from the last
    saved overnight scan(s) - see this module's own docstring.

    Returns either:
        {"available": False, "reason": "not_scanned"}
    or:
        {"available": True,
         "universe": str, "generated_at_label": str | None,
         "sector": str | None,
         "percentiles": {
             "value_score": {"universe": float | None, "sector": float | None},
             "quality":     {...}, "moat": {...}, "mos": {...},
             "psychology":  {...},
         },
         "peers": [
             {"ticker", "price", "intrinsic_value", "mos", "value_score",
              "quality", "moat"},
             ...  # up to PEER_TABLE_SIZE
         ]}
    """
    ticker = (ticker or "").strip().upper()
    if not ticker:
        return {"available": False, "reason": "not_scanned"}

    is_au = ticker.endswith(".AX")
    priority = _AU_PRIORITY if is_au else _US_PRIORITY
    all_universes = scanner_engine.AUSTRALIA_UNIVERSES if is_au else scanner_engine.USA_UNIVERSES

    universe, payload, own_row = _locate(ticker, priority, all_universes)
    if own_row is None:
        return {"available": False, "reason": "not_scanned"}

    rows = payload["rows"]
    sector = own_row.get("Sector") or None
    sector_rows = [r for r in rows if sector and r.get("Sector") == sector]

    percentiles = {}
    own_values = {}
    for field, key in _METRICS:
        own_value = own_row.get(field)
        own_values[key] = own_value
        percentiles[key] = {
            "universe": _percentile(own_value, [r.get(field) for r in rows]),
            "sector": (_percentile(own_value, [r.get(field) for r in sector_rows])
                       if sector else None),
        }

    return {
        "available": True,
        "universe": universe,
        "generated_at_label": payload.get("generated_at_label"),
        "sector": sector,
        "percentiles": percentiles,
        # The SAME row values the percentiles above were actually computed
        # from (this scan's own stored figures) - the UI should label a
        # chip with this, not a fresher live-computed number, so the
        # percentile shown always describes the value shown next to it.
        "own_values": own_values,
        "peers": _closest_peers(ticker, own_row, rows, sector),
    }
