"""
Ticker echo never a sentinel; positional fallback (owner-directed, 3
Oct 2026, after reading the Batch inspector on msgbatch_
01Jbz1uEmi1zesnrEbVHtchE): 43 requests, 0 errored, 41 with 5 items, 2
"empty" - the 2 "empty" ones actually contained company objects, just
with "ticker": "" (or missing) - the model applied the rubric's own
""-sentinel convention (meant for fields like inversion_scenario) to
the ticker ECHO field itself, mostly on all-zero NOT RATED companies.
69 companies were unmatched as a result.

Fix lives in top100_engine.py: one new sentence in _SYSTEM_PROMPT
telling the model never to blank the ticker echo; _parse_response_
json() gains an `expected_tickers` positional-fallback path (for a
blank-ticker item, only when the item COUNT matches the entrant
count - never guessed across a mismatch) and ticker-string
normalisation (_normalize_echoed_ticker(), dash/dot variants + a
missing ".AX" stem match) for an item that DID echo something, just
under a different string.

Run: python3 tests/test_top100_ticker_echo_fix.py
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import top100_engine as te


def _company_item(ticker, score=4):
    item = {k: {"score": score, "justification": "j", "source_period": "FY25"} for k in te.DIMENSION_KEYS}
    item.update({
        "ticker": ticker,
        "inversion_scenario": "", "inversion_severity": 0, "current_headwind": "",
        "market_structure": "", "market_structure_comment": "",
        "one_foot_hurdle": "", "one_foot_comment": "",
        "munger_quality": "", "munger_comment": "",
        "big_wave": "", "big_wave_comment": "",
    })
    return item


# ======================================================================
# CHECK 1: the system prompt tells the model never to sentinel the
# ticker echo - the exact root cause of the 2 Oct run's 69 unmatched
# companies.
# ======================================================================
assert "never leave it empty and never apply the empty-string sentinel to it" in te._SYSTEM_PROMPT
assert "The `ticker` field is an EXACT echo" in te._SYSTEM_PROMPT
print("[prompt_has_ticker_echo_sentence] _SYSTEM_PROMPT explicitly tells the model never to "
      "blank/sentinel the ticker echo field, including for a NOT RATED company OK")

# RUBRIC_VERSION is unchanged by this fix - it's a format instruction
# (how to fill a field already in the schema), not a scoring change,
# so no re-score should ever be triggered by this commit alone.
assert te.RUBRIC_VERSION == "v6", te.RUBRIC_VERSION
print(f"[rubric_version_unchanged] RUBRIC_VERSION is still {te.RUBRIC_VERSION!r} - this is a "
      "prompt-wording/parsing fix, not a scoring-rubric bump, so it never triggers a re-score OK")

# ======================================================================
# CHECK 2: blank-ticker items matched by position - 5 entrants, 5
# items, 2 of them (NOT RATED companies) echo a blank ticker - all 5
# still save, 2 via position, 3 via the ticker string as always.
# ======================================================================
_entrants_5 = ["AAA", "BBB", "CCC", "DDD", "EEE"]
_items_5 = [
    _company_item("AAA"),
    _company_item(""),       # blank - NOT RATED, position 1 -> BBB
    _company_item("CCC"),
    _company_item(None),     # missing entirely - position 3 -> DDD
    _company_item("EEE"),
]
_text_5 = json.dumps({"companies": _items_5})
_logged = []
_parsed_5 = te._parse_response_json(_text_5, expected_tickers=_entrants_5, log=_logged.append)
assert sorted(_parsed_5.keys()) == sorted(_entrants_5), _parsed_5.keys()
assert len(_logged) == 2, _logged  # one log line per positional match
assert any("BBB" in line and "position" in line for line in _logged), _logged
assert any("DDD" in line and "position" in line for line in _logged), _logged
print(f"[blank_ticker_matched_by_position] 5 entrants, 2 blank-ticker items (one empty string, "
      f"one missing key) -> all 5/5 saved, 2 via position (logged: {_logged}) OK")

# Without expected_tickers (the old call shape, still used wherever an
# entrant list genuinely isn't known), the same response's blank items
# are simply dropped, exactly as before this fix - no regression for a
# caller that doesn't opt in.
_parsed_5_noexp = te._parse_response_json(_text_5)
assert sorted(_parsed_5_noexp.keys()) == ["AAA", "CCC", "EEE"], _parsed_5_noexp.keys()
print("[no_expected_tickers_unchanged] without expected_tickers, blank-ticker items are still "
      "dropped (not guessed) exactly as before this fix OK")

# ======================================================================
# CHECK 3: count mismatch + blank tickers -> failures, nothing
# guessed. 4 items for 5 expected entrants, one of the 4 has a blank
# ticker - position can no longer be trusted (the item count doesn't
# match the entrant count), so that blank item is left unmatched, same
# as the genuinely-missing 5th entrant.
# ======================================================================
_items_4 = [
    _company_item("AAA"),
    _company_item(""),      # blank - would be position 1 -> BBB, but count differs, so NOT matched
    _company_item("CCC"),
    _company_item("EEE"),
]
_text_4 = json.dumps({"companies": _items_4})
_logged_mismatch = []
_parsed_4 = te._parse_response_json(_text_4, expected_tickers=_entrants_5, log=_logged_mismatch.append)
assert sorted(_parsed_4.keys()) == ["AAA", "CCC", "EEE"], _parsed_4.keys()
assert _logged_mismatch == [], _logged_mismatch  # no positional match attempted at all
# The caller (poll_and_ingest_batch's own per-ticker loop) would now
# record BOTH BBB and DDD as "missing_from_response" failures - never
# a guess across a count mismatch.
_missing = [t for t in _entrants_5 if t not in _parsed_4]
assert sorted(_missing) == ["BBB", "DDD"], _missing
print(f"[count_mismatch_never_guesses] 4 items for 5 expected entrants (one blank-ticker item "
      f"among the 4) -> only the 3 string-matched entrants saved, {_missing} left unmatched "
      "(recorded as missing_from_response as today) - position never attempted under a count "
      "mismatch OK")

# ======================================================================
# CHECK 4: "RG1" -> "RG1.AX" normalisation (dash/dot variants, missing
# ".AX" when exactly one entrant matches the stem) - both the direct
# helper and through _parse_response_json().
# ======================================================================
assert te._normalize_echoed_ticker("RG1", ["RG1.AX"]) == "RG1.AX"
assert te._normalize_echoed_ticker("rg1", ["RG1.AX"]) == "RG1.AX"  # case-insensitive
assert te._normalize_echoed_ticker("BRK-B", ["BRK.B"]) == "BRK.B"  # dash -> dot
assert te._normalize_echoed_ticker("BRK.B", ["BRK-B"]) == "BRK-B"  # dot -> dash
assert te._normalize_echoed_ticker("AAPL", ["AAPL"]) == "AAPL"      # already exact - untouched
# Ambiguous stem (two expected tickers share the same stem) - never
# guessed, left as the model's own literal string.
assert te._normalize_echoed_ticker("RG1", ["RG1.AX", "RG1.NZ"]) == "RG1"
print("[normalize_echoed_ticker] RG1->RG1.AX stem match, dash/dot variants, exact-match passthrough, "
      "and ambiguous-stem never guessed OK")

_text_rg1 = json.dumps({"companies": [_company_item("RG1")]})
_parsed_rg1 = te._parse_response_json(_text_rg1, expected_tickers=["RG1.AX"])
assert list(_parsed_rg1.keys()) == ["RG1.AX"], _parsed_rg1.keys()
print("[normalize_through_parse_response_json] a single-entrant request echoing 'RG1' resolves "
      "to 'RG1.AX' through the real parsing path, not just the standalone helper OK")

# ======================================================================
# CHECK 5: v5/v6 fixtures unchanged - a plain, correctly-ticked packed
# response (no blank tickers, no mismatches) behaves byte-for-byte as
# before this fix, confirming the new code paths are purely additive.
# ======================================================================
_clean_items = [_company_item(t) for t in _entrants_5]
_clean_text = json.dumps({"companies": _clean_items})
_clean_parsed = te._parse_response_json(_clean_text, expected_tickers=_entrants_5, log=lambda *a: (_ for _ in ()).throw(AssertionError("no positional match should ever log here")))
assert sorted(_clean_parsed.keys()) == sorted(_entrants_5), _clean_parsed.keys()
print("[v6_clean_response_unaffected] a fully-ticked packed response triggers zero positional "
      "matches and zero normalisation - identical behaviour to before this fix OK")

print("\nALL TOP 100 TICKER ECHO FIX FIXTURES PASSED")
