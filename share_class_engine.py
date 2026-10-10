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

# Audit fixes Commit 4 (30 Sep 2026, owner-directed - fb0e140 follow-up):
# a genuine second listed class rarely runs past 2-3x the primary
# class's own share count (even HEI/HEI.A, this module's own root-cause
# case, sits around 2.2x) - a candidate above this ceiling is far more
# likely a bad/stale Yahoo field (impliedSharesOutstanding in
# particular is not always populated correctly) than a real dual-class
# situation, and silently accepting it would understate every per-share
# figure computed from it instead of overstating - the opposite of the
# problem this whole module exists to fix. Rejected rather than
# clamped, so a caller sees plain sharesOutstanding (a known-real
# number) instead of a fabricated intermediate value.
DUAL_CLASS_SHARE_RATIO_MAX = 3.0

# Audit fix C2 / Fable finding V1 (10 Oct 2026, instruction_combined_
# 10oct.md PART C): a genuine DUAL-LISTED company - the SAME economic
# interest listed on two separate exchanges under two separate share
# registries (RIO Tinto plc/Limited's dual-listed structure, News
# Corp's ASX CDIs over its US-listed shares, James Hardie's and
# Amcor's own ASX-CHESS-over-primary-listing structures) - is a
# different situation from the dual-CLASS tickers above (SHARE_CLASS_
# PAIRS): info["sharesOutstanding"] on the ASX line here only counts
# that exchange's OWN register, not the combined group total, and that
# gap is routinely well past DUAL_CLASS_SHARE_RATIO_MAX's own 3x
# ceiling - unlike a genuine second share class, so the existing tiers
# below correctly reject it outright as "far more likely a bad Yahoo
# field". Explicit and hand-maintained, same precedent and reasoning as
# SHARE_CLASS_PAIRS above - never inferred, and never accepted on the
# ratio alone (see _dual_listed_group_shares()'s own marketCap/price
# cross-check, required precisely because this group is otherwise
# indistinguishable from "a bad Yahoo field" on the ratio test alone).
DUAL_LISTED_TICKERS = frozenset({"RIO.AX", "NWS.AX", "JHX.AX", "AMC.AX"})

# How closely Yahoo's own marketCap, divided by price, must agree with
# a DUAL_LISTED_TICKERS candidate for it to be trusted as the group
# total marketCap was actually computed from - independent corroboration
# standing in for DUAL_CLASS_SHARE_RATIO_MAX's own ceiling, which this
# path deliberately bypasses.
DUAL_LISTED_MARKETCAP_TOLERANCE = 0.10


def _dual_listed_group_shares(info, shares, ticker):
    """(group_shares_or_None, note_or_None) - only for a ticker on the
    explicit DUAL_LISTED_TICKERS list above, and only when info[
    "impliedSharesOutstanding"] clears DUAL_CLASS_SHARE_RATIO_MAX (the
    zone the ordinary tiers below reject outright) AND Yahoo's own
    marketCap/price independently corroborates it within DUAL_LISTED_
    MARKETCAP_TOLERANCE - never accepted on the ratio alone, since a
    ratio this extreme is otherwise indistinguishable from a bad Yahoo
    field (see DUAL_CLASS_SHARE_RATIO_MAX's own comment). Returns
    (None, None) - exactly like "not a candidate at all" elsewhere in
    this module - for every ticker not on the list, or where the
    corroboration check itself can't run (no implied/price/marketCap)
    or fails: the caller then falls through to the pre-existing tiers
    completely unchanged."""
    if ticker is None or ticker.strip().upper() not in DUAL_LISTED_TICKERS:
        return None, None
    implied = info.get("impliedSharesOutstanding")
    if not implied or implied <= shares * DUAL_CLASS_SHARE_RATIO_MAX:
        return None, None
    price = info.get("currentPrice") or info.get("regularMarketPrice") or info.get("previousClose")
    market_cap = info.get("marketCap")
    if not price or not market_cap:
        return None, None
    implied_from_cap = market_cap / price
    if abs(implied_from_cap - implied) > DUAL_LISTED_MARKETCAP_TOLERANCE * implied:
        return None, None
    return implied, (
        f"dual-listed group share count ({implied:,.0f}) used instead of this exchange's own "
        f"register ({shares:,.0f}) - corroborated by marketCap/price ({implied_from_cap:,.0f})"
    )

# How closely a corroborating filed diluted-shares figure (tier 3's own
# targeted fetch, reused here for verification rather than as a
# separate candidate) must agree with an "implied" candidate (tier 2)
# for that candidate to be trusted - only checked for a ticker already
# on the cheap, cached SHARE_CLASS_PAIRS list (see whole_company_
# shares()'s own docstring for why an arbitrary ticker outside that
# list has no free corroboration source and is accepted on the bounded
# ratio alone).
DUAL_CLASS_CORROBORATION_TOLERANCE = 0.15

_DILUTED_SHARES_ROW_NAMES = ("Diluted Average Shares", "Basic Average Shares")

# Sentinel distinguishing "_evaluate_candidate() was not asked to check
# corroboration at all" from "it was asked, and the corroborating fetch
# came back None" - the latter must still reject the candidate.
_UNCHECKED = object()


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


def _evaluate_candidate(candidate, shares, corroborated_by=_UNCHECKED):
    """(accepted_shares_or_None, note_or_None) for one whole-company
    share-count candidate against the ticker's own `shares` (info[
    "sharesOutstanding"]).

    - candidate missing, or its own ratio to `shares` doesn't even
      clear DUAL_CLASS_SHARE_RATIO: (None, None) - not a candidate at
      all, no note needed (the caller should keep trying its own next
      tier, exactly as before this fix).
    - ratio clears DUAL_CLASS_SHARE_RATIO but exceeds DUAL_CLASS_
      SHARE_RATIO_MAX (audit fixes Commit 4, 30 Sep 2026, owner-
      directed): rejected, with a note - a ratio this extreme is far
      more likely a bad Yahoo field than a real second share class.
    - `corroborated_by` (audit fixes Commit 4): when given (not the
      _UNCHECKED sentinel - None is a valid "no filed figure found"
      answer, distinct from "not checked at all"), the candidate is
      ALSO rejected, with a note, unless corroborated_by agrees with
      `candidate` within DUAL_CLASS_CORROBORATION_TOLERANCE. Omitted
      (the default) for a candidate that already IS a filed diluted-
      shares figure straight from an income statement (tiers "bundle"/
      "filed" below) - that figure needs no separate corroboration,
      it's already the authoritative source tier 2's own "implied"
      candidate would otherwise be checked against."""
    if not candidate or candidate <= shares * DUAL_CLASS_SHARE_RATIO:
        return None, None
    ratio = candidate / shares
    if candidate > shares * DUAL_CLASS_SHARE_RATIO_MAX:
        return None, (
            f"implied share count ignored (ratio {ratio:.1f}x, above the "
            f"{DUAL_CLASS_SHARE_RATIO_MAX:.0f}x sanity ceiling)"
        )
    if corroborated_by is not _UNCHECKED:
        if not corroborated_by or abs(corroborated_by - candidate) > (
                DUAL_CLASS_CORROBORATION_TOLERANCE * candidate):
            return None, f"implied share count ignored (ratio {ratio:.1f}x, uncorroborated)"
    return candidate, None


def whole_company_shares(info, income_df=None, ticker=None):
    """(shares, flagged, source, note). `shares` is info["sharesOutstanding"]
    unless a whole-company figure that clears DUAL_CLASS_SHARE_RATIO (and,
    as of audit fixes Commit 4, stays within DUAL_CLASS_SHARE_RATIO_MAX and
    - for the "implied" tier only - corroborates) was found; `flagged` is
    True whenever it was; `source` is "dual_listed_group" | "bundle" |
    "implied" | "filed" | None - "dual_listed_group" (audit fix C2 /
    Fable finding V1, 10 Oct 2026) is the one case that DELIBERATELY
    exceeds DUAL_CLASS_SHARE_RATIO_MAX, only for the explicit
    DUAL_LISTED_TICKERS list, only when marketCap/price corroborates -
    see that constant's own comment.
    `note` (new, audit fixes Commit 4, 30 Sep 2026, owner-directed) is a
    short explanation string whenever a candidate was FOUND but REJECTED
    (ratio above the sanity ceiling, or an "implied" candidate that a
    known ticker's own filed diluted count didn't corroborate) - None
    whenever no candidate existed at all, same as before this fix -
    EXCEPT "dual_listed_group" (above), which sets a disclosure note
    even on acceptance, per this task's own explicit instruction.
    (a caller like fcf_valuation_engine.dcf_intrinsic_value() threads
    this into meta["share_count_note"] rather than discarding it).

    income_df: pass a caller's own already-fetched income statement
    (auto_compounder_engine's bundle["income"]) when available - tier 1,
    zero new cost. Callers with no income statement on hand (nightly_
    scan/deep_dive_engine's lite path) pass None and fall through to
    tiers 2-3 instead.
    ticker: required for tier 3 (the cached targeted fetch needs a
    symbol to fetch) AND, as of Commit 4, to corroborate a tier-2
    "implied" candidate for a ticker on the cheap, cached SHARE_CLASS_
    PAIRS list - an arbitrary ticker outside that list has no free
    corroboration source, so a bound-clearing implied candidate for it
    is accepted on the ratio test alone (unchanged from before this
    fix). Omit ticker to stop after tier 2 uncorroborated - the auto_
    compounder_engine bundle path never needs tier 3, since tier 1 (its
    own income statement) already has the answer whenever one exists."""
    shares = info.get("sharesOutstanding") or 0
    if shares <= 0:
        return shares, False, None, None

    # Audit fix C2 / Fable finding V1 (10 Oct 2026): checked BEFORE
    # every tier below, for the small explicit DUAL_LISTED_TICKERS list
    # only - see that constant's and _dual_listed_group_shares()'s own
    # comments. Returns (None, None) for every other ticker, or when
    # the marketCap/price corroboration doesn't hold even for one of
    # these four - the caller then falls through to the pre-existing
    # tiers completely unchanged (e.g. a genuine income_df bundle for
    # one of these tickers, were a caller ever to supply one, is never
    # pre-empted by a check that didn't actually pass).
    _dl_shares, _dl_note = _dual_listed_group_shares(info, shares, ticker)
    if _dl_shares:
        return _dl_shares, True, "dual_listed_group", _dl_note

    if income_df is not None:
        filed = _row_from_income_df(income_df, _DILUTED_SHARES_ROW_NAMES)
        accepted, note = _evaluate_candidate(filed, shares)
        if accepted:
            return accepted, True, "bundle", None
        return shares, False, None, note

    implied = info.get("impliedSharesOutstanding")
    if implied:
        corroboration = _UNCHECKED
        known_ticker = ticker and ticker.strip().upper() in _KNOWN_MULTI_CLASS_TICKERS
        if known_ticker:
            corroboration = _fetch_diluted_shares(ticker.strip().upper())
        accepted, note = _evaluate_candidate(implied, shares, corroborated_by=corroboration)
        if accepted:
            return accepted, True, "implied", None
        if note:
            return shares, False, None, note

    if ticker and ticker.strip().upper() in _KNOWN_MULTI_CLASS_TICKERS:
        filed = _fetch_diluted_shares(ticker.strip().upper())
        accepted, note = _evaluate_candidate(filed, shares)
        if accepted:
            return accepted, True, "filed", None
        if note:
            return shares, False, None, note

    return shares, False, None, None
