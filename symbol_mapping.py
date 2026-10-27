"""
symbol_mapping.py

Stage 1a (3 Oct 2026, Director-directed, UK/Canada data layer), Step E -
pure, no-network Wikipedia->Yahoo ticker symbol mapping. Stage 1b (a
later, separate task) will call this to turn a symbol scraped off a
Wikipedia constituent list (e.g. "FTSE 100 constituents") into the
string Yahoo Finance actually expects in a request - NOTHING in this
codebase calls to_yahoo_symbol() or log_no_yahoo_price() yet; this
module exists on its own, ready for that caller, same as every other
building-block this stage adds (the GBp pence normalisation, the GBP/
CAD FX and risk-free-rate work) ahead of Stage 1b actually fetching any
new universe.

No network, no yfinance/requests import, no side effects - pure string
transforms only, so this is trivially unit-testable without a sandbox
network constraint of any kind.
"""

# Explicit override dict - checked FIRST, before any transform or
# idempotency check, per the Director's own instruction. Empty today
# (no known case needs one yet) - Stage 1b's own job to populate this
# as real Wikipedia-vs-Yahoo mismatches turn up that the plain
# transform rules below don't already handle (e.g. a company that
# changed its Yahoo symbol independently of its LSE/TSX ticker, or a
# genuinely irregular one neither ".L"/".TO" suffixing nor the dot-to-
# hyphen rule gets right).
SYMBOL_OVERRIDES = {}


def _lse_stem(raw):
    """LSE-specific stem transform, BEFORE the ".L" suffix is appended:
    a trailing dot is stripped ("RR." -> "RR"), THEN any remaining
    (inner) dot becomes a hyphen ("BT.A" -> "BT-A"). Order matters - a
    symbol can't have both in these examples, but stripping the
    trailing dot first and only then hyphenating whatever's left keeps
    the two rules from interacting in a surprising way if one ever
    did."""
    s = raw
    if s.endswith("."):
        s = s[:-1]
    return s.replace(".", "-")


def _tsx_stem(raw):
    """TSX-specific stem transform, BEFORE the ".TO" suffix is
    appended: every dot becomes a hyphen ("BAM.A" -> "BAM-A", "RCI.B"
    -> "RCI-B", "CAR.UN" -> "CAR-UN"). No trailing-dot rule - unlike
    LSE, nothing in this task's own worked examples shows a TSX symbol
    ending in a bare dot."""
    return raw.replace(".", "-")


# PART 6 STEP 6.1 (8 Oct 2026, Director-directed, instruction_health_
# fixes_chart_and_new_markets.md): the five European markets' own
# Yahoo suffixes - standard, well-known Yahoo Finance exchange codes,
# never a memorised ticker LIST (that still only ever comes from a
# live fetch - see scanner_engine.py's own fetch_dax()/fetch_cac40()/
# fetch_aex()/fetch_smi()/fetch_omxs30()). Reuses the SAME generic
# "strip a trailing dot, hyphenate any remaining inner dot" stem rule
# _lse_stem() already applies for LSE - a safe, idempotent default for
# any exchange's own share-class dot notation, unverified against real
# constituent data for these five specifically until the first live
# scan (same disclosure as every other unverified live feed Stage 1b/
# PART 6 adds).
_EUROPEAN_EXCHANGE_SUFFIX = {
    "XETRA": ".DE", "EURONEXT_PARIS": ".PA", "EURONEXT_AMSTERDAM": ".AS",
    "SIX": ".SW", "NASDAQ_STOCKHOLM": ".ST",
}

# Director's overnight queue TASK 1 (14 Oct 2026). Root cause (verified
# in production logs, Rescan 2026-10-14 04:17-04:18 UTC): a Wikipedia
# constituent table sometimes lists a company by the raw Yahoo symbol
# of its OWN primary listing on a DIFFERENT exchange, not a bare code
# for the universe's own exchange - e.g. CAC 40's table lists
# ArcelorMittal (whose real Yahoo symbol is "MT.AS", Euronext
# Amsterdam) and DAX's table lists Airbus (whose real Yahoo symbol is
# "AIR.PA", Euronext Paris). The OLD per-exchange idempotency check
# only recognised the CURRENT exchange's own suffix, so it treated the
# foreign suffix as an inner class-share dot, hyphenated it, and
# appended the current exchange's suffix on top - "MT.AS" under
# EURONEXT_PARIS became "MT-AS.PA" (not a real Yahoo symbol); "AIR.PA"
# under XETRA became "AIR-PA.DE" - exactly the two no-Yahoo-price
# symbols the rescan logged.
#
# General fix: every suffix this module ever appends, in one set.
# `raw` already ending in ANY of them (not just the CURRENT exchange's
# own) means it is already a complete, valid Yahoo symbol for whatever
# market it actually trades on - returned unchanged, regardless of
# which exchange's universe it was scraped from. This replaces (never
# duplicates) the old per-branch "already ends with MY suffix" checks
# below, since checking against every suffix already covers the
# current exchange's own case too.
_ALL_YAHOO_SUFFIXES = (".L", ".TO", ".T", ".DE", ".PA", ".AS", ".SW", ".ST")


def to_yahoo_symbol(raw, exchange, overrides=None):
    """Pure function, no network: `raw` (a symbol as scraped off a
    Wikipedia constituent table) -> the Yahoo Finance symbol for it,
    given `exchange` ("LSE", "TSX", "TSE", "XETRA", "EURONEXT_PARIS",
    "EURONEXT_AMSTERDAM", "SIX" or "NASDAQ_STOCKHOLM" - any other value
    raises ValueError, since there's no rule to apply).

    Order of checks:
      1. `overrides` (defaults to SYMBOL_OVERRIDES) - checked first,
         against `raw` exactly as given (after only a leading/trailing-
         whitespace strip). A hit here is returned verbatim, before
         any transform or idempotency check runs.
      2. Idempotent, and NOT limited to the current `exchange`
         (Director's overnight queue TASK 1, 14 Oct 2026): if `raw`
         already ends in ANY of this module's own Yahoo suffixes -
         _ALL_YAHOO_SUFFIXES (".L"/".TO"/".T"/".DE"/".PA"/".AS"/".SW"/
         ".ST") - case-insensitive check, original casing returned -
         it's returned UNCHANGED. A symbol scraped off ONE universe's
         Wikipedia table sometimes already carries the complete, valid
         Yahoo symbol of its real listing on a DIFFERENT exchange (e.g.
         CAC 40's table listing ArcelorMittal as "MT.AS", its actual
         Euronext Amsterdam symbol) - checking every suffix, not just
         the current exchange's own, means that case is recognised and
         left alone instead of having a second suffix stacked onto it.
      3. LSE: strip a trailing dot, hyphenate any remaining inner dot,
         append ".L" - see _lse_stem()'s own docstring.
      4. TSX: hyphenate every dot, append ".TO" - see _tsx_stem()'s own
         docstring.
      5. TSE (Stage 1 Japan, 3 Oct 2026, Director-directed): plain
         suffix append, no stem transform - a Tokyo Stock Exchange code
         (numeric, "7203", or alphanumeric, "130A") has no dot to strip
         or hyphenate, unlike an LSE/TSX symbol.
      6. XETRA/EURONEXT_PARIS/EURONEXT_AMSTERDAM/SIX/NASDAQ_STOCKHOLM
         (PART 6 STEP 6.1): same stem transform as LSE (strip a
         trailing dot, hyphenate any remaining inner dot - covers a
         Swedish A/B share class like "VOLV A" -> "VOLV-A.ST"), then
         the exchange's own suffix from _EUROPEAN_EXCHANGE_SUFFIX.

    Examples: to_yahoo_symbol("RR.", "LSE") == "RR.L";
    to_yahoo_symbol("BT.A", "LSE") == "BT-A.L";
    to_yahoo_symbol("BAM.A", "TSX") == "BAM-A.TO";
    to_yahoo_symbol("RCI.B", "TSX") == "RCI-B.TO";
    to_yahoo_symbol("CAR.UN", "TSX") == "CAR-UN.TO";
    to_yahoo_symbol("RR.L", "LSE") == "RR.L" (idempotent - unchanged);
    to_yahoo_symbol("7203", "TSE") == "7203.T";
    to_yahoo_symbol("130A", "TSE") == "130A.T";
    to_yahoo_symbol("7203.T", "TSE") == "7203.T" (idempotent - unchanged);
    to_yahoo_symbol("SAP", "XETRA") == "SAP.DE";
    to_yahoo_symbol("MC", "EURONEXT_PARIS") == "MC.PA";
    to_yahoo_symbol("ASML", "EURONEXT_AMSTERDAM") == "ASML.AS";
    to_yahoo_symbol("NESN", "SIX") == "NESN.SW";
    to_yahoo_symbol("VOLV.B", "NASDAQ_STOCKHOLM") == "VOLV-B.ST";
    to_yahoo_symbol("MT.AS", "EURONEXT_PARIS") == "MT.AS" (ArcelorMittal's
    real symbol is a different exchange's - returned unchanged, never
    "MT-AS.PA"); to_yahoo_symbol("AIR.PA", "XETRA") == "AIR.PA" (Airbus's
    real symbol is a different exchange's - returned unchanged, never
    "AIR-PA.DE")."""
    overrides = overrides if overrides is not None else SYMBOL_OVERRIDES
    raw = (raw or "").strip()
    if raw in overrides:
        return overrides[raw]

    upper_raw = raw.upper()
    if any(upper_raw.endswith(suffix) for suffix in _ALL_YAHOO_SUFFIXES):
        return raw

    if exchange == "LSE":
        return f"{_lse_stem(raw)}.L"
    if exchange == "TSX":
        return f"{_tsx_stem(raw)}.TO"
    if exchange == "TSE":
        return f"{raw}.T"
    if exchange in _EUROPEAN_EXCHANGE_SUFFIX:
        suffix = _EUROPEAN_EXCHANGE_SUFFIX[exchange]
        return f"{_lse_stem(raw)}{suffix}"
    # TASK 22 (Director-directed, 27 Oct 2026, "Foreign US-listed"): a
    # foreign-domiciled company listed directly on NYSE/Nasdaq/NYSE
    # American carries no Yahoo COUNTRY suffix at all (unlike every
    # exchange above) - its Yahoo symbol is just the US ticker itself,
    # dots hyphenated the same way every other exchange's own stem rule
    # already does (e.g. "BRK.B" -> "BRK-B").
    if exchange == "US":
        return raw.replace(".", "-")

    raise ValueError(
        f"to_yahoo_symbol: unknown exchange {exchange!r} - expected 'LSE', 'TSX', 'TSE', "
        "'XETRA', 'EURONEXT_PARIS', 'EURONEXT_AMSTERDAM', 'SIX', 'NASDAQ_STOCKHOLM' or 'US'"
    )


def log_no_yahoo_price(index_name, yahoo_symbol, wiki_symbol, log=print):
    """The log line Stage 1b will call (nothing calls this yet - see
    this module's own docstring) when a symbol it derived via
    to_yahoo_symbol() from a Wikipedia constituent table turns out to
    have no Yahoo price available - i.e. the mapping produced a string
    Yahoo doesn't actually recognise, which is exactly the signal that
    SYMBOL_OVERRIDES above needs a new entry for this one.

    Format: '[symbols] <index_name>: no Yahoo price for <yahoo_symbol>
    (wiki "<wiki_symbol>")' - e.g. '[symbols] FTSE 100: no Yahoo price
    for XYZ.L (wiki "XYZ")'."""
    log(f'[symbols] {index_name}: no Yahoo price for {yahoo_symbol} (wiki "{wiki_symbol}")')
