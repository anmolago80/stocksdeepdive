"""Budget Ledger STEP 4 (9 Oct 2026, Director-directed): B, bank CSV
import - pure parsing/cleaning/suggestion/dedup engine
(ledger_import_engine.py), tested against the instruction's own exact
fictional fixture rows.

Run: python3 tests/test_budget_ledger_step4_import_engine.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ledger_import_engine as lie

passed = 0
failed = 0


def check(label, condition):
    global passed, failed
    if condition:
        passed += 1
        print(f"  OK: {label}")
    else:
        failed += 1
        print(f"  FAIL: {label}")


# Fictional fixture rows, exactly as given in instruction_budget_ledger.md.
FIXTURE_CSV = (
    '07/10/2026,"-48.10","WOOLWORTHS      1234     SAMPLEVILLE QL",""\n'
    '06/10/2026,"-201.55","AGL SALES PTY LTD        SYDNEY",""\n'
    '05/10/2026,"-26.99","AMAZON AU RETAIL         SYDNEY",""\n'
    '05/10/2026,"-26.99","AMAZON AU RETAIL         SYDNEY",""\n'
    '04/10/2026,"-4.92","INTNL TRANSACTION FEE",""\n'
    '04/10/2026,"-140.50","EXAMPLE ESIM             AMSTERDAM   NH ## NLD MERCHANT",""\n'
    '03/10/2026,"-15.71","SAMPLE SOFTWARE          SAN FRANCISCCA ##0926          11.00 US DOLLAR",""\n'
    '03/10/2026,"-20.00","SQ *SAMPLE CAFE          Newtown     QL",""\n'
    '02/10/2026,"-1.00","TRANSLINK TICKETING      QLD",""\n'
    '17/09/2026,"+8995.48","PAYMENT RECEIVED, THANK YOU",""\n'
    '16/09/2026,"+35.00","AMAZON AU RETAIL         SYDNEY",""\n'
)

# ======================================================================
# CHECK 1-2: layout detection + full parse of the real fixture.
# ======================================================================
_layout, _cols = lie.detect_layout(FIXTURE_CSV)
check("the fixture's own layout is detected as headerless_4col", _layout == "headerless_4col")
check("column count is 4", _cols == 4)

_parsed = lie.parse_bank_csv(FIXTURE_CSV)
check("all 11 fixture rows parse", len(_parsed["rows"]) == 11)
check("dates are day-first (07/10/2026 -> 2026-10-07, never 2026-07-10)",
      _parsed["rows"][0]["date"] == "2026-10-07")

# ======================================================================
# CHECK 3: a headered file (Date/Amount/Description, case-insensitive).
# ======================================================================
_headered_csv = 'Date,Amount,Description\n07/10/2026,"-48.10","WOOLWORTHS SAMPLEVILLE"\n'
_h_layout, _h_cols = lie.detect_layout(_headered_csv)
check("a headered Date/Amount/Description file is recognised", _h_layout == "headered")
_h_parsed = lie.parse_bank_csv(_headered_csv)
check("the headered file's one row parses correctly",
      len(_h_parsed["rows"]) == 1 and _h_parsed["rows"][0]["date"] == "2026-10-07")

# ======================================================================
# CHECK 4: an unrecognised layout - never a guess, reports column count.
# ======================================================================
_weird_csv = "col1,col2,col3,col4,col5\na,b,c,d,e\n"
_w_layout, _w_cols = lie.detect_layout(_weird_csv)
check("an unrecognised 5-column layout returns layout=None", _w_layout is None)
check("...and reports the real column count (5), never a guess", _w_cols == 5)

# ======================================================================
# CHECK 5: classify every fixture row - merchant cleaning, repayment,
# fee, foreign-fx, refund-vs-purchase sign.
# ======================================================================
_classified = [lie.classify_parsed_row(r) for r in _parsed["rows"]]
_by_desc = {c["raw_description"]: c for c in _classified}

_woolworths = _by_desc["WOOLWORTHS      1234     SAMPLEVILLE QL"]
check("WOOLWORTHS: merchant cleaned to 'WOOLWORTHS'", _woolworths["merchant"] == "WOOLWORTHS")
check("WOOLWORTHS: a purchase (-48.10) becomes positive spending (4810 cents)",
      _woolworths["amount_cents"] == 4810)
check("WOOLWORTHS: rule_key is lower-cased", _woolworths["rule_key"] == "woolworths")

_fee = _by_desc["INTNL TRANSACTION FEE"]
check("INTNL TRANSACTION FEE: detected as a fee row", _fee["is_fee"] is True)
check("INTNL TRANSACTION FEE: still a normal spend amount (492 cents)", _fee["amount_cents"] == 492)

_foreign = _by_desc["SAMPLE SOFTWARE          SAN FRANCISCCA ##0926          11.00 US DOLLAR"]
check("foreign purchase: note_fx is 'US$11.00' exactly", _foreign["note_fx"] == "US$11.00")
check("foreign purchase: the AUD amount (15.71) is what's stored, not the USD one",
      _foreign["amount_cents"] == 1571)
check("foreign purchase: merchant is still cleaned correctly ('SAMPLE SOFTWARE')",
      _foreign["merchant"] == "SAMPLE SOFTWARE")

_unmapped_foreign = _by_desc["EXAMPLE ESIM             AMSTERDAM   NH ## NLD MERCHANT"]
check("a ##MMYY marker with NO parseable trailing amount+currency -> note_fx is None "
      "(never a guess)", _unmapped_foreign["note_fx"] is None)

_sq_cafe = _by_desc["SQ *SAMPLE CAFE          Newtown     QL"]
check("SQ *SAMPLE CAFE: the DISPLAY merchant keeps the 'SQ *' prefix",
      _sq_cafe["merchant"] == "SQ *SAMPLE CAFE")
check("SQ *SAMPLE CAFE: the RULE KEY strips the card-processor prefix",
      _sq_cafe["rule_key"] == "sample cafe")

_repayment = _by_desc["PAYMENT RECEIVED, THANK YOU"]
check("PAYMENT RECEIVED: detected as a card repayment", _repayment["is_card_repayment"] is True)
check("PAYMENT RECEIVED: amount_cents is None - never saved", _repayment["amount_cents"] is None)

_refund = _by_desc["AMAZON AU RETAIL         SYDNEY"]
# NOTE: two rows share this exact raw_description (the two $26.99 same-
# day twins) plus the $35.00 refund - dict collapses to last one parsed
# (the refund, "16/09/2026,+35.00"), which is fine for this one check.
check("a genuine refund (positive, not 'PAYMENT RECEIVED') is saved as a NEGATIVE amount",
      _refund["amount_cents"] == -3500)
check("a refund is never flagged as a card repayment", _refund["is_card_repayment"] is False)

# Same-day twins - BOTH $26.99 AMAZON rows must survive classification
# (legitimate within-file duplicates, per the instruction's own rule).
_amazon_purchases = [c for c in _classified if c["merchant"] == "AMAZON AU RETAIL" and c["amount_cents"] == 2699]
check("same-day identical $26.99 AMAZON rows: both survive classification (2, not 1)",
      len(_amazon_purchases) == 2)

# ======================================================================
# CHECK 6: suggestions - precedence (your rule > built-in > needs you),
# and the built-in list's own "exact category name match" rule.
# ======================================================================
_woolworths_category, _woolworths_source = lie.suggest_category(
    "woolworths", lambda k: None, ["Food & groceries", "Groceries"],
)
check('WOOLWORTHS matches the built-in list under "Groceries" when the user HAS that '
      "exact category", _woolworths_category == "Groceries" and _woolworths_source == "built-in")

_woolworths_no_match_category, _woolworths_no_match_source = lie.suggest_category(
    "woolworths", lambda k: None, ["Food & groceries"],  # the REAL preset label, not "Groceries"
)
check('WOOLWORTHS falls through to "needs you" when the user\'s only category is the real '
      'preset label ("Food & groceries"), not an exact "Groceries" match - the disclosed, '
      "real consequence of the fixed-10-preset category set",
      _woolworths_no_match_category is None and _woolworths_no_match_source == "needs you")

_rule_category, _rule_source = lie.suggest_category(
    "woolworths", lambda k: "My Custom Category" if k == "woolworths" else None, [],
)
check("the user's OWN rule takes precedence over the built-in list entirely",
      _rule_category == "My Custom Category" and _rule_source == "your rule")

_unknown_category, _unknown_source = lie.suggest_category(
    "sample software", lambda k: None, ["Subscriptions"],
)
check('an unrecognised merchant ("sample software") is "needs you" even with a plausible '
      "category available", _unknown_category is None and _unknown_source == "needs you")

_liberty_category, _liberty_source = lie.suggest_category(
    "liberty", lambda k: None, ["Transport", "Fun, eating out, hobbies"],
)
check('the deliberately-ambiguous "LIBERTY" (fuel or shop) is NOT in the built-in list at all',
      _liberty_source == "needs you")

# ======================================================================
# CHECK 7: dedup across imports - same fingerprint only imports the
# DIFFERENCE between what's in the file and what's already stored.
# ======================================================================
_rows_for_dedup = [
    {"date": "2026-10-07", "amount_cents": 4810, "raw_description": "WOOLWORTHS A"},
    {"date": "2026-10-05", "amount_cents": 2699, "raw_description": "AMAZON"},
    {"date": "2026-10-05", "amount_cents": 2699, "raw_description": "AMAZON"},  # same-day twin
]
_fp_amazon = lie.fingerprint(_rows_for_dedup[1])
# Simulate: one of the two AMAZON rows was already imported earlier.
_new_rows, _skipped = lie.dedupe_against_existing(_rows_for_dedup, {_fp_amazon: 1})
check("dedup: the already-imported AMAZON row is skipped (1 skipped)", _skipped == 1)
check("dedup: exactly 2 new rows survive (WOOLWORTHS + the SECOND Amazon twin)",
      len(_new_rows) == 2)
check("dedup: the surviving Amazon row is still a real row (not corrupted/dropped wrongly)",
      any(r["raw_description"] == "AMAZON" for r in _new_rows))

# Overlapping files test: import the SAME 3 rows again - this time ALL
# should be skipped (all already stored from the "first" import above).
_existing_after_first_import = {}
for r in _rows_for_dedup:
    fp = lie.fingerprint(r)
    _existing_after_first_import[fp] = _existing_after_first_import.get(fp, 0) + 1
_new_rows2, _skipped2 = lie.dedupe_against_existing(_rows_for_dedup, _existing_after_first_import)
check("re-importing the exact same file a second time: everything is skipped (3 skipped, "
      "0 new)", _skipped2 == 3 and len(_new_rows2) == 0)

print()
print(f"PASS={passed} FAIL={failed}")
sys.exit(1 if failed else 0)
