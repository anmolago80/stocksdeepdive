"""
ledger_import_engine.py

Budget Ledger STEP 4 (9 Oct 2026, Director-directed): B, bank CSV
import - pure-logic parsing/cleaning/suggestion/dedup engine behind the
Ledger tab's "Import from bank" button. Same "no network, no
Streamlit, no file I/O" rule as budget_planner_engine.py - this module
takes plain text/strings in, returns plain dicts/lists out, so it's
trivially unit-testable and the caller (app.py) owns the actual
st.file_uploader widget and the tools_store.* persistence calls.

PRIVACY (instruction's own section 0 rule): this module never writes
anything anywhere - no logging, no file writes. The caller is
responsible for logging only counts (tools_store.record_ledger_import),
never a row's own description/amount.

CATEGORY MATCHING - BY CATEGORY ID, NOT LABEL TEXT (10 Oct 2026,
Director-directed fix): BUILTIN_SUGGESTIONS and ledger_rules (the
"your rule" path - tools_store.get_merchant_rule/upsert_merchant_rule
both already store the category ID, e.g. "food", never a display
label) are both matched and returned BY THE FIXED CATEGORY ID from
budget_planner_engine.CATEGORY_IDS, then translated to the caller's
own display label via the `cat_id_to_label` map passed into
suggest_category(). This replaces an earlier build that matched the
built-in list's own category NAMES ("Groceries", "Insurance", ...)
against the user's display labels ("Food & groceries", "Insurances")
as case-insensitive exact text - which silently never matched, since
Step 2's 10 fixed preset labels don't exact-match those names, so
every built-in suggestion fell through to "needs you" even for an
obviously-recognised merchant.

One builtin group has no sensible 1:1 preset and is INTENTIONALLY
left unmapped, per the instruction's own rule ("merchant groups with
no sensible preset stay 'needs you'"): "Shopping" (AMAZON AU) could be
groceries, electronics, a gift, anything - there is no single preset
category it honestly belongs to. See BUILTIN_SUGGESTIONS' own mapping
table below for every other group's chosen preset id.
"""
import csv
import io
import re
from datetime import datetime


# ---------------------------------------------------------------------
# 4.1 Format parsing
# ---------------------------------------------------------------------

_CARD_REPAYMENT_MARKER = "PAYMENT RECEIVED"
_INTNL_FEE_DESCRIPTION = "INTNL TRANSACTION FEE"
_FOREIGN_MARKER_RE = re.compile(r"##(\d{2})(\d{2})")
_FOREIGN_AMOUNT_RE = re.compile(r"(\d[\d,]*\.\d{2})\s+([A-Z][A-Z ]*[A-Z])\s*$")
_CARD_PROCESSOR_PREFIXES = ("SQ *", "SMP*", "LS ", "ZLR*")

_CURRENCY_NAME_TO_SYMBOL = {
    "US DOLLAR": "US$", "US DOLLARS": "US$",
    "UK POUND": "£", "POUND STERLING": "£", "GBP": "£",
    "EURO": "€", "EUROS": "€",
    "NZ DOLLAR": "NZ$", "NZ DOLLARS": "NZ$",
    "CANADIAN DOLLAR": "CA$", "CANADIAN DOLLARS": "CA$",
    "JAPANESE YEN": "¥", "YEN": "¥",
}


def _parse_date_ddmmyyyy(raw):
    """DD/MM/YYYY -> "YYYY-MM-DD", or None if unparseable. Always day
    first, never MM/DD, per the instruction's own explicit rule."""
    try:
        return datetime.strptime(raw.strip(), "%d/%m/%Y").date().isoformat()
    except (ValueError, AttributeError):
        return None


def _parse_signed_amount(raw):
    """A quoted signed decimal, e.g. "-72.25" or "+8995.48", possibly
    with thousands commas. Returns a float, or None if unparseable."""
    try:
        return float(str(raw).strip().replace(",", ""))
    except (ValueError, TypeError):
        return None


def detect_layout(csv_text):
    """"headerless_4col" (the Commonwealth-Bank-style export this
    module's own fixture matches), "headered" (a Date/Amount/
    Description-named header row - case-insensitive), or None (layout
    not recognised - the caller shows "This file layout isn't
    recognised yet" plus the column count, never a guess)."""
    reader = csv.reader(io.StringIO(csv_text))
    rows = [r for r in reader if r]
    if not rows:
        return None, 0
    first = rows[0]
    _lower = [c.strip().lower() for c in first]
    if "date" in _lower and "amount" in _lower and "description" in _lower:
        return "headered", len(first)
    if len(first) == 4 and _parse_date_ddmmyyyy(first[0]) and _parse_signed_amount(first[1]) is not None:
        return "headerless_4col", len(first)
    return None, len(first)


def parse_bank_csv(csv_text):
    """{"layout": "headerless_4col"|"headered"|None, "column_count":
    int, "rows": [{"date", "amount", "raw_description", "balance"}]}.
    `rows` is empty and `layout` is None when the file layout isn't
    recognised - the caller is responsible for the "not recognised"
    message, this function never guesses a shape it can't confirm."""
    layout, col_count = detect_layout(csv_text)
    if layout is None:
        return {"layout": None, "column_count": col_count, "rows": []}

    reader = csv.reader(io.StringIO(csv_text))
    raw_rows = [r for r in reader if r]
    rows = []
    if layout == "headerless_4col":
        for r in raw_rows:
            if len(r) != 4:
                continue
            _date = _parse_date_ddmmyyyy(r[0])
            _amount = _parse_signed_amount(r[1])
            if _date is None or _amount is None:
                continue
            rows.append({
                "date": _date, "amount": _amount, "raw_description": r[2], "balance": r[3] or None,
            })
    else:  # headered
        header = [c.strip().lower() for c in raw_rows[0]]
        _di = header.index("date")
        _ai = header.index("amount")
        _desci = header.index("description")
        for r in raw_rows[1:]:
            if len(r) <= max(_di, _ai, _desci):
                continue
            _date = _parse_date_ddmmyyyy(r[_di])
            _amount = _parse_signed_amount(r[_ai])
            if _date is None or _amount is None:
                continue
            rows.append({
                "date": _date, "amount": _amount, "raw_description": r[_desci], "balance": None,
            })
    return {"layout": layout, "column_count": col_count, "rows": rows}


# ---------------------------------------------------------------------
# 4.2 Cleaning
# ---------------------------------------------------------------------

def clean_merchant(raw_description):
    """The first segment before a run of 2+ spaces, trimmed - the
    DISPLAY merchant (raw_description itself is kept as-is by the
    caller; this is only the cleaned merchant name)."""
    return re.split(r"\s{2,}", (raw_description or "").strip(), maxsplit=1)[0].strip()


def merchant_rule_key(merchant):
    """Lower-cased, with card-processor prefixes stripped - the
    lookup key for ledger_rules/the built-in starter list ONLY. The
    DISPLAY merchant (clean_merchant's own return value) never has
    these prefixes stripped."""
    m = merchant
    for prefix in _CARD_PROCESSOR_PREFIXES:
        if m.upper().startswith(prefix):
            m = m[len(prefix):]
            break
    return m.strip().lower()


def extract_foreign_fx(raw_description):
    """"US$11.00", or None if this row has no ##MMYY foreign-purchase
    marker. The amount column is already in AUD (that's what's
    stored) - this is purely the "what it originally cost" note.
    An unrecognised currency name is shown as "<amount> <NAME>"
    verbatim (honest - never guessing a symbol this module doesn't
    know) rather than silently dropping the information."""
    if not _FOREIGN_MARKER_RE.search(raw_description or ""):
        return None
    m = _FOREIGN_AMOUNT_RE.search(raw_description)
    if not m:
        return None
    amount, currency_name = m.group(1), m.group(2).strip()
    symbol = _CURRENCY_NAME_TO_SYMBOL.get(currency_name.upper())
    if symbol:
        return f"{symbol}{amount}"
    return f"{amount} {currency_name}"


def is_card_repayment(raw_description):
    """A positive row whose description contains "PAYMENT RECEIVED" -
    never counted, shown greyed out, not saved (instruction's own
    explicit rule)."""
    return _CARD_REPAYMENT_MARKER in (raw_description or "").upper()


def is_intnl_transaction_fee(raw_description):
    return (raw_description or "").strip().upper() == _INTNL_FEE_DESCRIPTION


def classify_parsed_row(row):
    """Classifies one parsed row ({"date","amount","raw_description",
    "balance"}) into a dict ready for suggestion/review:
    {"date", "amount_cents" (positive = spend, per tools_store's own
    convention), "merchant", "rule_key", "raw_description", "note_fx",
    "is_card_repayment", "is_fee"}. amount_cents is None for a card
    repayment row (never saved, per the instruction's own rule)."""
    merchant = clean_merchant(row["raw_description"])
    is_repayment = row["amount"] > 0 and is_card_repayment(row["raw_description"])
    if is_repayment:
        amount_cents = None
    elif row["amount"] < 0:
        # A purchase - stored as positive spending.
        amount_cents = round(-row["amount"] * 100)
    else:
        # A refund (positive, not a repayment) - saved as a negative amount.
        amount_cents = -round(row["amount"] * 100)
    return {
        "date": row["date"],
        "amount_cents": amount_cents,
        "merchant": merchant,
        "rule_key": merchant_rule_key(merchant),
        "raw_description": row["raw_description"],
        "note_fx": extract_foreign_fx(row["raw_description"]),
        "is_card_repayment": is_repayment,
        "is_fee": is_intnl_transaction_fee(row["raw_description"]),
    }


# ---------------------------------------------------------------------
# 4.3 Suggestions (rules only, no AI)
# ---------------------------------------------------------------------

# Director's own built-in starter list, now keyed by the FIXED PRESET
# CATEGORY ID (budget_planner_engine.CATEGORY_IDS) each merchant group
# maps to - not by the built-in group's own old display name. Kept
# here as a reviewable data structure, per the instruction's own "kept
# in a reviewable data file" requirement. Ambiguous names (e.g.
# LIBERTY - fuel or shop) are deliberately NOT in this list.
#
# Mapping table (old built-in group -> preset id -> preset label):
#   Groceries         -> food          -> "Food & groceries"
#   Transport         -> transport     -> "Transport (car, petrol, fares)"
#   Utilities         -> utilities     -> "Utilities (power, water, internet, phone)"
#   Phone & internet  -> utilities     -> "Utilities (power, water, internet, phone)" (merged - no separate preset)
#   Health            -> health        -> "Health"
#   Insurance         -> insurance     -> "Insurances"
#   Subscriptions     -> subscriptions -> "Subscriptions"
#   Shopping          -> (none)        -> no sensible preset - stays "needs you"
BUILTIN_SUGGESTIONS = {
    "food": ["WOOLWORTHS", "COLES", "ALDI", "IGA"],
    "transport": ["TRANSLINK", "OPAL", "MYKI", "CALTEX", "BP", "AMPOL", "SHELL", "7-ELEVEN", "CELLOPARK"],
    "utilities": [
        "AGL", "ORIGIN ENERGY", "ENERGYAUSTRALIA", "ALINTA",
        "TPG", "TELSTRA", "OPTUS", "VODAFONE", "AUSSIE BROADBAND",
    ],
    "health": ["CHEMIST WAREHOUSE", "PRICELINE"],
    "insurance": ["SUNCORP INSURANCE", "YOUI", "NRMA", "RACQ", "AAMI", "BUDGET DIRECT", "ALLIANZ"],
    "subscriptions": ["APPLE.COM/BILL", "MICROSOFT", "NETFLIX", "SPOTIFY"],
    # "Shopping" (AMAZON AU) intentionally has no entry here - no single
    # preset category honestly fits general shopping - falls through
    # to "needs you".
}


def _builtin_category_for_merchant(rule_key):
    """(category_id, matched) for the built-in list - matches if
    rule_key STARTS WITH (or equals) one of the listed merchant
    strings, case-insensitively (e.g. "sample software" doesn't match
    anything here; "amazon au retail" matches nothing - "Shopping" is
    deliberately not in BUILTIN_SUGGESTIONS)."""
    upper_key = rule_key.upper()
    for category_id, merchants in BUILTIN_SUGGESTIONS.items():
        for m in merchants:
            if upper_key.startswith(m.upper()):
                return category_id, True
    return None, False


def suggest_category(rule_key, user_rule_lookup, cat_id_to_label):
    """(label, source) - source is "your rule" | "built-in" | "needs
    you". `user_rule_lookup(rule_key)` is a callable the caller passes
    (normally tools_store.get_merchant_rule bound to the signed-in
    email) - this module never imports tools_store itself, keeping the
    "no file I/O" rule. It returns a category ID (the same ID
    ledger_rules/upsert_merchant_rule store), or None.
    `cat_id_to_label`: {category_id: display_label} for the user's
    real preset categories (budget_planner_engine.CATEGORIES). Both
    "your rule" and the built-in list are matched BY CATEGORY ID
    against each other and against BUILTIN_SUGGESTIONS, then
    translated to the caller's own display label here - never by
    matching label text (see this module's own docstring for why the
    old text-matching approach silently never fired)."""
    own_rule_id = user_rule_lookup(rule_key)
    if own_rule_id and own_rule_id in (cat_id_to_label or {}):
        return cat_id_to_label[own_rule_id], "your rule"
    builtin_id, matched = _builtin_category_for_merchant(rule_key)
    if matched and builtin_id in (cat_id_to_label or {}):
        return cat_id_to_label[builtin_id], "built-in"
    return None, "needs you"


# ---------------------------------------------------------------------
# 4.4 Duplicates
# ---------------------------------------------------------------------

def fingerprint(row):
    """(date, amount_cents, raw_description) - the dedup key. Same-day
    identical rows WITHIN one file are legitimate and kept (the
    instruction's own explicit rule) - this fingerprint is only used
    to dedup ACROSS imports, via counting, never to drop a row inside
    a single file."""
    return (row["date"], row["amount_cents"], row["raw_description"])


def dedupe_against_existing(classified_rows, existing_fingerprint_counts):
    """For each fingerprint, import only (count in this file minus
    count already stored from earlier imports) - same-fingerprint
    rows within this file are processed in order, and only the ones
    beyond what's already stored are marked new. Returns (new_rows,
    skipped_count) - `new_rows` preserves the original order and
    shape of `classified_rows`, just filtered.
    `existing_fingerprint_counts`: {fingerprint: count_already_stored},
    the caller's job to compute from already-saved rows."""
    seen_this_file = {}
    new_rows = []
    skipped = 0
    for row in classified_rows:
        fp = fingerprint(row)
        seen_this_file[fp] = seen_this_file.get(fp, 0) + 1
        already_stored = existing_fingerprint_counts.get(fp, 0)
        if seen_this_file[fp] <= already_stored:
            skipped += 1
        else:
            new_rows.append(row)
    return new_rows, skipped
