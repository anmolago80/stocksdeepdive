"""
share_class_engine.py

Data-correctness audit A3b (27 Sep 2026, owner-directed): the ONE place
a dual/multi-class company's whole-company share count is resolved from
- shared by every valuation path on the site: nightly_scan's lite DCF
(via resolver_engine -> fcf_valuation_engine, driving the site's actual
Scanner/Deep Dive/Top 100 Intrinsic Value and MOS), auto_compounder_
engine's own Rational Compounder DCF/BVPS/Equity Method 10y/IV-BV
series, and moat_engine's WACC equity weight (via auto_compounder_
engine._basics()).

A dual-class ticker's own info["sharesOutstanding"] can reflect only
ONE listed class - HEICO's "HEI" Common shows 55,235,561 shares against
a whole-company diluted total around 122M (see this module's own
whole_company_shares() docstring below, and auto_compounder_engine.py's
original HEI/HEICO root-cause comment) - which understates every
per-share figure computed from it. A3 (5e1ec4a) fixed this inside
auto_compounder_engine.py, which always has a full fundamentals bundle
(including the income statement) already fetched, so its own correction
is a zero-network-call local test. nightly_scan/deep_dive_engine's lite
DCF path has no income-statement fetch of its own, and deliberately
never got one (see fcf_valuation_engine.dcf_intrinsic_value's own
docstring on diluted_shares_override) - adding a per-ticker income-
statement fetch to EVERY nightly scan (thousands of tickers) and every
live Scanner/Comparison DCF was rejected as too expensive, on the same
day this repo's own commits dealt with a Yahoo rate-limit incident from
exactly that kind of added per-ticker load.

Three-tier resolution here, cheapest first, each tier only reached if
the previous one didn't resolve it:
  1. A caller-supplied income statement DataFrame (auto_compounder_
     engine's bundle["income"], already fetched for other reasons) -
     zero new cost, reads the same "Diluted Average Shares" row A3's
     own fix already used.
  2. info["impliedSharesOutstanding"] - already present on the SAME
     `info` dict every caller already has (nightly_scan/deep_dive_
     engine fetch `info` regardless), so this is also a zero-network-
     call check. Not every ticker has this field populated, but when
     Yahoo does populate it with a genuine whole-company total, it's
     free.
  3. SHARE_CLASS_PAIRS (below) - an explicit, hand-maintained set of
     known multi-class tickers on this site (the same list top100_
     engine.py's own pre-scoring dedupe uses, and for the identical
     reason: never inferred from company-name similarity). Only for a
     ticker in this small set, and only when tiers 1-2 didn't already
     resolve it, a CACHED, single-ticker targeted fetch (never a whole-
     universe fetch) reads just that ticker's own income statement for
     the same row. Cached at a long TTL, same convention as capm_
     engine.get_risk_free_rate's own multi-hour cache in this same
     process - a whole night's scan touching the same handful of known
     dual-class tickers costs a handful of calls total, not one per
     scan run.

Every OTHER ticker (the overwhelming majority of every universe this
site scans) never reaches tier 3 at all - identical cost to before this
module existed.
"""

import streamlit as st
import yfinance as yf

# Single source of truth for every known multi-class ticker group on
# the site - top100_engine.py's own pre-scoring dedupe (_dedupe_share_
# classes) imports this list rather than keeping its own copy, so the
# two can't drift apart. Deliberately explicit and hand-maintained,
# never inferred from company-name similarity (see top100_engine.py's
# own _report_undocumented_same_name_duplicates() for why: even
# DETECTING a merge candidate this way was ruled out, not just merging
# on it). A ticker not listed here never reaches tier 3 above, however
# similar its company name looks to another row's.
SHARE_CLASS_PAIRS = [
    frozenset({"GOOG", "GOOGL"}),          # Alphabet Inc.
    frozenset({"FOX", "FOXA"}),            # Fox Corporation
    frozenset({"NWS", "NWSA"}),            # News Corporation
    frozenset({"BRK-A", "BRK-B"}),         # Berkshire Hathaway
    frozenset({"HEI", "HEI-A"}),           # HEICO Corporation
]

_KNOWN_MULTI_CLASS_TICKERS = frozenset(
    t for pair in SHARE_CLASS_PAIRS for t in pair
)

# A candidate whole-company share count must exceed the ticker's own
# reported sharesOutstanding by more than this multiple to be trusted
# as a genuine second share class rather than ordinary buyback/issuance
# drift (typically single-digit-to-low-teens percent over a fiscal
# year) - same threshold auto_compounder_engine.py's original dual-
# class mcap fix used, kept as the ONE number both that fix and this
# module's own tiers now share.
DUAL_CLASS_SHARE_RATIO = 1.3

_DILUTED_SHARES_ROW_NAMES = ("Diluted Average Shares", "Basic Average Shares")


def _row_from_income_df(income_df, row_names):
    if income_df is None or income_df.empty:
        return None
    for name in row_names:
        if name in income_df.index:
            row = income_df.loc[name]
            for v in row:
                if v is not None and v == v:  # not NaN
                    return float(v)
    # Fuzzy fallback, same convention as auto_compounder_engine._find_row.
    lower_idx = {str(i).lower(): i for i in income_df.index}
    for name in row_names:
        nl = name.lower()
        for li, orig in lower_idx.items():
            if nl in li or li in nl:
                row = income_df.loc[orig]
                for v in row:
                    if v is not None and v == v:
                        return float(v)
    return None


@st.cache_data(ttl=21600, show_spinner=False)
def _fetch_diluted_shares(ticker):
    """Tier 3's own targeted, single-ticker fetch - ONLY ever called for
    a ticker in SHARE_CLASS_PAIRS (see whole_company_shares() below),
    never for the general population. 6-hour cache: this doesn't move
    intra-day, and the whole point is that a night's worth of nightly-
    scan calls against the same handful of known tickers collapses to
    one real fetch per ticker per cache window, not one per scan."""
    try:
        income = yf.Ticker(ticker).income_stmt
    except Exception:
        return None
    return _row_from_income_df(income, _DILUTED_SHARES_ROW_NAMES)


def whole_company_shares(info, income_df=None, ticker=None):
    """(shares, flagged, source). `shares` is info["sharesOutstanding"]
    unless a whole-company figure that clears DUAL_CLASS_SHARE_RATIO was
    found; `flagged` is True whenever it was; `source` is "bundle" |
    "implied" | "filed" | None.

    income_df: pass a caller's own already-fetched income statement
    (auto_compounder_engine's bundle["income"]) when available - tier 1,
    zero new cost. Callers with no income statement on hand (nightly_
    scan/deep_dive_engine's lite path) pass None and fall through to
    tiers 2-3 instead.
    ticker: required for tier 3 only (the cached targeted fetch needs a
    symbol to fetch); omit it (or omit income_df's absence entirely) to
    stop after tier 2 - the auto_compounder_engine bundle path never
    needs tier 3, since tier 1 (its own income statement) already has
    the answer whenever one exists."""
    shares = info.get("sharesOutstanding") or 0
    if shares <= 0:
        return shares, False, None

    if income_df is not None:
        filed = _row_from_income_df(income_df, _DILUTED_SHARES_ROW_NAMES)
        if filed and filed > shares * DUAL_CLASS_SHARE_RATIO:
            return filed, True, "bundle"
        return shares, False, None

    implied = info.get("impliedSharesOutstanding")
    if implied and implied > shares * DUAL_CLASS_SHARE_RATIO:
        return implied, True, "implied"

    if ticker and ticker.strip().upper() in _KNOWN_MULTI_CLASS_TICKERS:
        filed = _fetch_diluted_shares(ticker.strip().upper())
        if filed and filed > shares * DUAL_CLASS_SHARE_RATIO:
            return filed, True, "filed"

    return shares, False, None
