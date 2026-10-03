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


def to_yahoo_symbol(raw, exchange, overrides=None):
    """Pure function, no network: `raw` (a symbol as scraped off a
    Wikipedia constituent table) -> the Yahoo Finance symbol for it,
    given `exchange` ("LSE" or "TSX" - any other value raises
    ValueError, since there's no rule to apply).

    Order of checks:
      1. `overrides` (defaults to SYMBOL_OVERRIDES) - checked first,
         against `raw` exactly as given (after only a leading/trailing-
         whitespace strip). A hit here is returned verbatim, before
         any transform or idempotency check runs.
      2. Idempotent: if `raw` already ends with the exchange's own
         Yahoo suffix (".L" for LSE, ".TO" for TSX - case-insensitive
         check, original casing returned), it's returned UNCHANGED -
         calling this on an already-Yahoo-shaped symbol is always a
         safe no-op, never a double suffix.
      3. LSE: strip a trailing dot, hyphenate any remaining inner dot,
         append ".L" - see _lse_stem()'s own docstring.
      4. TSX: hyphenate every dot, append ".TO" - see _tsx_stem()'s own
         docstring.

    Examples: to_yahoo_symbol("RR.", "LSE") == "RR.L";
    to_yahoo_symbol("BT.A", "LSE") == "BT-A.L";
    to_yahoo_symbol("BAM.A", "TSX") == "BAM-A.TO";
    to_yahoo_symbol("RCI.B", "TSX") == "RCI-B.TO";
    to_yahoo_symbol("CAR.UN", "TSX") == "CAR-UN.TO";
    to_yahoo_symbol("RR.L", "LSE") == "RR.L" (idempotent - unchanged)."""
    overrides = overrides if overrides is not None else SYMBOL_OVERRIDES
    raw = (raw or "").strip()
    if raw in overrides:
        return overrides[raw]

    if exchange == "LSE":
        if raw.upper().endswith(".L"):
            return raw
        return f"{_lse_stem(raw)}.L"
    if exchange == "TSX":
        if raw.upper().endswith(".TO"):
            return raw
        return f"{_tsx_stem(raw)}.TO"

    raise ValueError(f"to_yahoo_symbol: unknown exchange {exchange!r} - expected 'LSE' or 'TSX'")


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
