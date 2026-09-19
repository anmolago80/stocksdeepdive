"""
ticker_search_engine.py

Matching logic for the site-wide nav search box (/search, /es/search -
see server.py's search_page()/search_suggest() routes). Reads only
already-cached scan data via snapshot_store.all_public_rows() - no new
API call, live fetch or third-party lookup of any kind. A tiny
in-process cache (_CACHE_TTL_SECONDS) keeps a search box on every page
from turning into a SQLite query on every keystroke's suggest() call.
"""

import difflib
import time

import snapshot_store

_CACHE_TTL_SECONDS = 300
_cache = {"rows": None, "at": 0.0}


def _corpus():
    now = time.time()
    if _cache["rows"] is None or (now - _cache["at"]) > _CACHE_TTL_SECONDS:
        rows = []
        for r in snapshot_store.all_public_rows():
            ticker = (r.get("ticker") or "").strip()
            if not ticker:
                continue
            rows.append({
                "ticker": ticker,
                "company_name": (r.get("company_name") or "").strip(),
                "universe": (r.get("universe") or "").strip(),
            })
        _cache["rows"] = rows
        _cache["at"] = now
    return _cache["rows"]


def _norm(s):
    return (s or "").strip().lower()


def search(query, limit=50):
    """Case-insensitive, partial match against ticker AND company name.
    Symmetric substring containment (query-in-ticker OR ticker-in-query)
    on the ticker side so a query typed with an exchange suffix the
    stored ticker doesn't carry (or vice versa - "CBA" vs "CBA.AX")
    still matches directly, without falling through to suggest()'s fuzzy
    path. Ranked so an exact ticker match always sorts first (callers
    use rank 0 to decide a single confident redirect)."""
    q = _norm(query)
    if not q:
        return []
    scored = []
    for row in _corpus():
        t = _norm(row["ticker"])
        n = _norm(row["company_name"])
        if q == t:
            rank = 0
        elif t and (q in t or t in q):
            rank = 1
        elif n and n.startswith(q):
            rank = 2
        elif n and q in n:
            rank = 3
        else:
            continue
        scored.append((rank, row["ticker"], row))
    scored.sort(key=lambda x: (x[0], x[1]))
    return [row for _rank, _ticker, row in scored[:limit]]


def suggest(query, limit=6):
    """"Closest matches" for a query with no direct search() hit - real
    typo tolerance (e.g. a misspelled company name), not the suffix
    mismatch case above (search() already covers that directly)."""
    q = _norm(query)
    if not q:
        return []
    corpus = _corpus()
    by_ticker = {_norm(r["ticker"]): r for r in corpus}
    by_name = {_norm(r["company_name"]): r for r in corpus if r["company_name"]}
    close = (difflib.get_close_matches(q, by_ticker.keys(), n=limit, cutoff=0.6)
             + difflib.get_close_matches(q, by_name.keys(), n=limit, cutoff=0.6))
    seen = set()
    out = []
    for key in close:
        row = by_ticker.get(key) or by_name.get(key)
        if row and row["ticker"] not in seen:
            seen.add(row["ticker"])
            out.append(row)
        if len(out) >= limit:
            break
    return out
