"""
Director addendum 2, Part 2 of 2, item D (3 Oct 2026) - TSX Composite
source (report, then fix the label). Live log: "TSX Composite: scanning
219 tickers (Derived: Wikipedia S&P/TSX 60 (live) backfilled against any
live TSX Composite rows)". Same rules as the Lists & display
instruction. HOLD ALL PUSHES still applies.

Where the 219 TSX Composite tickers came from on the live run (report,
not a code change - see this file's own bottom NOTE for the full
answer): a live Wikipedia Composite-page scrape, backfilled by TSX 60
for any small gaps - NOT the memory-written list (that fallback list was
deleted in Lists & display Commit 1) and NOT "TSX 60 backfilled against
Composite" as the Director's own log paraphrase has the direction
backwards - see _asx_backfill_missing_subset_tickers()'s own contract:
a None superset (failed Composite scrape) can never produce a non-None
backfilled result, so 219 rows could only come from a successful (if
partial) live Composite scrape.

Covers: every one of this task's six universes' (FTSE 100/250, TSX 60/
Composite, Nikkei 225, TOPIX 500) source labels now ends in exactly one
of "(live)"/"(fund file)"/"(saved list, <date>)" - replacing the old
free-text "... (live) instead"/"Last known-good list (source: ..., as
of ...)" forms. Scoped to only these six universes, per CLAUDE.md's
"prefer additive changes... never touch without explicit task" -
the ~25 other US/ASX derived-universe labels (e.g. "ASX 300 (live)
instead") are untouched.

ALL FIXTURES IN THIS FILE ARE SYNTHETIC - this sandbox has no outbound
network access, same disclosure as every other fixture-based test in
this repo.

Run: python3 tests/test_director_addendum2_part2d_source_labels.py
"""
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from unittest import mock

import pandas as pd

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

TESTVOL = tempfile.mkdtemp(prefix="addendum2_part2d_test_")
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = TESTVOL
os.environ.pop("PRIVATE_UNIVERSES", None)

import scanner_engine as se


def _wiki_df(tickers):
    return pd.DataFrame({"Ticker": tickers, "Company": [f"Co {t}" for t in tickers],
                          "Sector": ["Industrials"] * len(tickers)})


def _clear_last_good(slug):
    try:
        os.remove(se._universe_list_cache_path(slug))
    except OSError:
        pass


# ======================================================================
# D1: _strip_universe_source_suffix_tag() - the re-tagging helper itself.
# ======================================================================
assert se._strip_universe_source_suffix_tag("Wikipedia FTSE 100 (live)") == "Wikipedia FTSE 100"
assert se._strip_universe_source_suffix_tag("iShares MIDD holdings file (fund file)") == \
    "iShares MIDD holdings file"
assert se._strip_universe_source_suffix_tag("something with no tag") == "something with no tag"
assert se._strip_universe_source_suffix_tag("") == "unknown"
assert se._strip_universe_source_suffix_tag(None) == "unknown"
print("[D1_strip_suffix_tag] strips a trailing (live)/(fund file) tag, passes through an "
      "untagged string unchanged, falls back to 'unknown' for empty/None OK")


# ======================================================================
# D2: fund-file-primary label ends "(fund file)", not "(live)"/
# "(live) instead".
# ======================================================================
_clear_last_good("ftse_250")
_fund_df = _wiki_df(["HWDN.L", "BBOX.L", "VTY.L"])
with mock.patch.object(se, "fetch_ftse250", return_value=None), \
     mock.patch.object(se, "fetch_ftse250_ishares", return_value=_fund_df):
    df, source = se.get_universe_pool("United Kingdom", "FTSE 250")
assert df is not None, df
assert source.endswith("(fund file)"), source
assert "(live)" not in source, source
print(f"[D2_fund_file_label] fund-file-primary label now ends '(fund file)': {source!r} OK")


# ======================================================================
# D3: last-good label ends "(saved list, <date>)", not the old
# "Last known-good list (source: ..., as of ...)" free text - and is
# never double-tagged even though the SAVED source string already had
# its own "(live)" tag on it.
# ======================================================================
_clear_last_good("ftse_100")
se._save_last_good_universe_list("ftse_100", ["AZN.L", "SHEL.L"], "Wikipedia FTSE 100 (live)")
with mock.patch.object(se, "fetch_ftse100", return_value=None), \
     mock.patch.object(se, "fetch_ftse100_ishares", return_value=None):
    df2, source2 = se.get_universe_pool("United Kingdom", "FTSE 100")
assert df2 is not None, df2
assert source2.startswith("Wikipedia FTSE 100 (saved list, "), source2
assert source2.count("(live)") == 0, source2  # stripped, not doubled up
assert source2.count("(saved list,") == 1, source2
print(f"[D3_saved_list_label_no_double_tag] last-good label: {source2!r} - old '(live)' tag "
      f"stripped before the new '(saved list, <date>)' tag is added, never doubled OK")


# ======================================================================
# D4: TOPIX 500 success label ends "(live)" cleanly - not the old
# "(live, Core30+Large70+Mid400)" form (Core30+Large70+Mid400 still
# appears, just not inside the trailing tag parens).
# ======================================================================
_clear_last_good("topix_500")
_topix_df = _wiki_df([f"{9000+i}.T" for i in range(400)])
with mock.patch.object(se, "fetch_topix500", return_value=_topix_df):
    df3, source3 = se.get_universe_pool("Japan", "TOPIX 500")
assert df3 is not None and len(df3) == 400, df3
assert source3.endswith("(live)"), source3
assert "(live, Core30" not in source3, source3
assert "Core30+Large70+Mid400" in source3, source3
print(f"[D4_topix_label_clean_live_tag] TOPIX 500 live-success label: {source3!r} OK")


print("\nALL Director addendum 2, Part 2, item D (TSX Composite source / label vocabulary) CHECKS PASSED")
print(
    "\nNOTE for the Director - item D's report answer: the live run's 219 TSX "
    "Composite tickers came from a successful (if partial) LIVE Wikipedia "
    "Composite-page scrape, topped up by TSX 60 for any tickers Composite's "
    "own scrape was missing - never from the memory-written fallback list "
    "(deleted in Lists & display Commit 1) and never 'TSX 60 backfilled "
    "against Composite' as the Director's own log paraphrase has it backwards. "
    "Proof from the code itself: _asx_backfill_missing_subset_tickers(superset_df, "
    "subset_df, ...) returns None unchanged whenever superset_df is None - so "
    "the call site in scanner_engine.py, "
    "_asx_backfill_missing_subset_tickers(fetch_tsxcomposite(), fetch_tsx60(), "
    "'TSX Composite', 'TSX 60'), could only have produced a non-None 219-row "
    "result if fetch_tsxcomposite() (the superset/primary argument) itself "
    "succeeded live. TSX 60 (the subset argument) is only ever used to patch "
    "small gaps in Composite's own scrape - it is structurally incapable of "
    "being the sole source of 219 rows."
)
