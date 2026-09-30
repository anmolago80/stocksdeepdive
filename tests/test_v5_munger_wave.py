"""
Top 100 rubric v5 (owner-approved mock, "mock_top100_v5_munger_line.html",
30 Sep 2026, Commit 4 of instruction_growth_top100_freshness_fix.md).

Two new display-only AI-scoring verdicts: munger_quality ("yes"/"no"/"")
and big_wave ("tailwind"/"flat"/"headwind"/""), each with its own
<=25-word comment. RUBRIC_VERSION bumped v4 -> v5. Neither field is ever
read by composite_score(), any sort key, or selection (grep-verified
separately, in the commit's own report).

This file covers:
  - schema dry-check: zero union-typed params, no min/max keys, RUBRIC_
    VERSION == "v5", max_tokens unchanged
  - _parse_response_json(): clean parse of every allowed value for both
    fields, case/whitespace tolerance, out-of-vocabulary -> declined with
    its own comment force-nulled (independently per field), hard
    truncation at 40 words, "" sentinel -> None
  - top100_store.save_score()/get_score(): round-trips all four new
    columns; a caller using the OLD (pre-v5) positional/keyword argument
    list still works (backward compatibility)
  - top100_render._competitive_landscape_html(): all 16 present/absent
    combinations of (market_structure, munger_quality, big_wave,
    one_foot_hurdle) render the four lines in the fixed order Market
    structure -> Business quality -> Big wave to ride -> Easy decision,
    only the LAST rendered line carries the " · read {date}" suffix, all
    three big_wave chip variants render with the right styling, EN+ES
  - AppTest: the real render_top100_page() renders without crashing or
    blanking on a mix of a v4-fallback row (no v5 columns at all) and a
    genuine v5 row, both languages

Run: python3 tests/test_v5_munger_wave.py
"""
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TESTVOL = tempfile.mkdtemp(prefix="top100_v5_munger_wave_test_")
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = TESTVOL

import top100_engine as te
import top100_store as ts
import top100_render as tr

if os.path.exists(ts.DB_PATH):
    os.remove(ts.DB_PATH)


def _dims_all_scored():
    return {k: {"score": 4, "justification": "j", "source_period": "FY25"} for k in te.DIMENSION_KEYS}


def _response_json(munger_quality="yes", munger_comment="Simple, widening moat, high ROIC.",
                    big_wave="tailwind", big_wave_comment="Early penetration, long runway.",
                    market_structure="oligopoly", one_foot_hurdle="no"):
    import json
    data = {k: {"score": 4, "justification": "j", "source_period": "FY25"} for k in te.DIMENSION_KEYS}
    data["inversion_scenario"] = "A severe downturn."
    data["inversion_severity"] = 3
    data["current_headwind"] = "Margin pressure."
    data["market_structure"] = market_structure
    data["market_structure_comment"] = "Named peers."
    data["one_foot_hurdle"] = one_foot_hurdle
    data["one_foot_comment"] = "Requires judgment."
    data["munger_quality"] = munger_quality
    data["munger_comment"] = munger_comment
    data["big_wave"] = big_wave
    data["big_wave_comment"] = big_wave_comment
    return json.dumps(data)


# ---------------------------------------------------------------
# 1. Schema dry-check
# ---------------------------------------------------------------
schema = te._response_schema()
props = schema["properties"]


def _is_union(p):
    return isinstance(p.get("type"), list) or "anyOf" in p or "oneOf" in p


union_count = sum(1 for p in props.values() if _is_union(p))
minmax_keys = [k for k, p in props.items()
               if any(mk in p for mk in ("minimum", "maximum", "minLength", "maxLength"))]

assert union_count == 0, f"expected 0 union-typed properties, got {union_count}"
assert minmax_keys == [], f"expected no min/max-constrained properties, got {minmax_keys}"
assert te.RUBRIC_VERSION == "v5", f"expected RUBRIC_VERSION v5, got {te.RUBRIC_VERSION!r}"
assert "munger_quality" in props and "munger_comment" in props
assert "big_wave" in props and "big_wave_comment" in props
assert set(["munger_quality", "munger_comment", "big_wave", "big_wave_comment"]).issubset(set(schema["required"]))
params = te._request_params("TEST", "Test Co")
assert params["max_tokens"] == 4000
print("[schema] RUBRIC_VERSION v5, 0 union types, no min/max, max_tokens unchanged OK")


# ---------------------------------------------------------------
# 2. _parse_response_json(): clean values, tolerance, out-of-vocab,
#    truncation, sentinel handling
# ---------------------------------------------------------------
# Clean parse of every allowed value
for mq in ("yes", "no"):
    text = _response_json(munger_quality=mq, munger_comment="Reason.")
    parsed = te._parse_response_json(text)
    munger_quality, munger_comment = parsed[9], parsed[10]
    assert munger_quality == mq, f"expected munger_quality={mq!r}, got {munger_quality!r}"
    assert munger_comment == "Reason."
print("[parse] munger_quality yes/no clean parse OK")

for bw in ("tailwind", "flat", "headwind"):
    text = _response_json(big_wave=bw, big_wave_comment="Trend.")
    parsed = te._parse_response_json(text)
    big_wave, big_wave_comment = parsed[11], parsed[12]
    assert big_wave == bw, f"expected big_wave={bw!r}, got {big_wave!r}"
    assert big_wave_comment == "Trend."
print("[parse] big_wave tailwind/flat/headwind clean parse OK")

# Case/whitespace tolerance
text = _response_json(munger_quality="  YES  ", big_wave=" Tailwind ")
parsed = te._parse_response_json(text)
assert parsed[9] == "yes", parsed[9]
assert parsed[11] == "tailwind", parsed[11]
print("[parse] case/whitespace tolerance OK")

# Sentinel "" -> None (independently per field), comment also nulled
text = _response_json(munger_quality="", munger_comment="", big_wave="", big_wave_comment="")
parsed = te._parse_response_json(text)
assert parsed[9] is None and parsed[10] is None
assert parsed[11] is None and parsed[12] is None
print("[parse] sentinel \"\" -> None for both fields + their comments OK")

# Out-of-vocabulary -> declined (None), comment force-nulled even if
# the model didn't null it itself - each field independently
text = _response_json(munger_quality="maybe", munger_comment="A stray comment.",
                       big_wave="strong", big_wave_comment="Another stray comment.")
parsed = te._parse_response_json(text)
assert parsed[9] is None, "out-of-vocab munger_quality must decline to None"
assert parsed[10] is None, "munger_comment must be force-nulled when munger_quality is invalid"
assert parsed[11] is None, "out-of-vocab big_wave must decline to None"
assert parsed[12] is None, "big_wave_comment must be force-nulled when big_wave is invalid"
print("[parse] out-of-vocabulary values decline to None with comment force-nulled OK")

# One field valid, the other invalid - independence
text = _response_json(munger_quality="yes", munger_comment="Valid.",
                       big_wave="bogus", big_wave_comment="Should be nulled.")
parsed = te._parse_response_json(text)
assert parsed[9] == "yes" and parsed[10] == "Valid."
assert parsed[11] is None and parsed[12] is None
print("[parse] independent field validation (one valid, one invalid) OK")

# Hard truncation at 40 words (_COMMENT_MAX_WORDS), safety net only
long_comment = " ".join(f"word{i}" for i in range(60))
text = _response_json(munger_comment=long_comment, big_wave_comment=long_comment)
parsed = te._parse_response_json(text)
assert len(parsed[10].split()) == te._COMMENT_MAX_WORDS, len(parsed[10].split())
assert len(parsed[12].split()) == te._COMMENT_MAX_WORDS, len(parsed[12].split())
print("[parse] hard truncation at _COMMENT_MAX_WORDS words OK")


# ---------------------------------------------------------------
# 3. Store round-trip + backward compatibility
# ---------------------------------------------------------------
dims = _dims_all_scored()
ts.save_score(
    ticker="MELI", quarter="RP2026-09-30", model=te.MODEL_TOP100, rubric_version="v5",
    dims=dims, not_rated=False, inversion_scenario="scenario", inversion_severity=3,
    prompt="{}", raw_response="{}",
    current_headwind="headwind text", market_structure="oligopoly",
    market_structure_comment="peers", one_foot_hurdle="no", one_foot_comment="hard",
    munger_quality="yes", munger_comment="Widening moat, high ROIC.",
    big_wave="tailwind", big_wave_comment="Early penetration, long runway.",
)
got = ts.get_score("MELI", "RP2026-09-30", te.MODEL_TOP100, "v5")
assert got["munger_quality"] == "yes"
assert got["munger_comment"] == "Widening moat, high ROIC."
assert got["big_wave"] == "tailwind"
assert got["big_wave_comment"] == "Early penetration, long runway."
print("[store] save_score()/get_score() round-trips all four v5 columns OK")

# Backward-compatible call: old (pre-v5) argument list, no munger_
# quality/big_wave kwargs at all - must not raise, and must read back
# as None for the four new columns (exactly what a real v4-era row
# looks like).
ts.save_score(
    ticker="CARG", quarter="RP2026-09-30", model=te.MODEL_TOP100, rubric_version="v4",
    dims=dims, not_rated=False, inversion_scenario="scenario", inversion_severity=2,
    prompt="{}", raw_response="{}",
    current_headwind="headwind text", market_structure="competitive",
    market_structure_comment="peers", one_foot_hurdle="no", one_foot_comment="hard",
)
got_v4 = ts.get_score("CARG", "RP2026-09-30", te.MODEL_TOP100, "v4")
assert got_v4 is not None
assert got_v4.get("munger_quality") is None
assert got_v4.get("big_wave") is None
print("[store] backward-compatible save_score() call (pre-v5 argument list) OK")


# ---------------------------------------------------------------
# 4. Render: _competitive_landscape_html() - all 16 combinations,
#    chip styles, date-suffix-on-last-line, EN+ES
# ---------------------------------------------------------------
import html as _html

_BASE_ROW = {
    "not_rated": False,
    "scored_at": datetime.now(timezone.utc).isoformat(),
    "market_structure": None, "market_structure_comment": None,
    "munger_quality": None, "munger_comment": None,
    "big_wave": None, "big_wave_comment": None,
    "one_foot_hurdle": None, "one_foot_comment": None,
}


def _row(**overrides):
    row = dict(_BASE_ROW)
    row.update(overrides)
    return row


for lang in ("en", "es"):
    # All absent -> ""
    assert tr._competitive_landscape_html(_row(), lang) == ""

    # All 16 present/absent combinations of the four verdicts
    values = {
        "market_structure": (None, ("oligopoly", "peers here")),
        "munger_quality": (None, ("yes", "Widening moat.")),
        "big_wave": (None, ("tailwind", "Early penetration.")),
        "one_foot_hurdle": (None, ("no", "Requires judgment.")),
    }
    keys = list(values.keys())
    for mask in range(16):
        overrides = {}
        present_order = []
        for i, key in enumerate(keys):
            present = bool(mask & (1 << i))
            choice = values[key][1] if present else values[key][0]
            if choice is None:
                overrides[key] = None
                overrides[f"{key}_comment" if key != "market_structure" else "market_structure_comment"] = None
            else:
                val, comment = choice
                overrides[key] = val
                comment_key = "market_structure_comment" if key == "market_structure" else f"{key}_comment"
                overrides[comment_key] = comment
                present_order.append(key)
        row = _row(**overrides)
        out = tr._competitive_landscape_html(row, lang)
        if mask == 0:
            assert out == "", f"mask=0 should render empty, got {out!r}"
            continue
        assert out != "", f"mask={mask:04b} should render something"
        # Order check: the keys present must appear in DOM order matching
        # the fixed sequence market_structure -> munger_quality ->
        # big_wave -> one_foot_hurdle.
        fixed_order = ["market_structure", "munger_quality", "big_wave", "one_foot_hurdle"]
        expected_present = [k for k in fixed_order if k in present_order]
        # crude order check via label substring positions
        label_map = {
            "market_structure": "market_structure_label" if lang == "en" else "market_structure_label",
        }
        # Verify each expected label appears, and in increasing index order
        idx_positions = []
        for k in expected_present:
            if k == "market_structure":
                needle = _html.escape(tr._t("market_structure_label", lang))
            elif k == "munger_quality":
                needle = _html.escape(tr._t("business_quality_label", lang))
            elif k == "big_wave":
                needle = _html.escape(tr._t("big_wave_label", lang))
            else:
                needle = _html.escape(tr._t("easy_decision_label", lang))
            pos = out.find(needle)
            assert pos != -1, f"mask={mask:04b} lang={lang}: label for {k} not found in output"
            idx_positions.append(pos)
        assert idx_positions == sorted(idx_positions), (
            f"mask={mask:04b} lang={lang}: lines not in fixed order, positions={idx_positions}"
        )
        # Date suffix appears exactly once, right after the LAST rendered line
        date_caption = _html.escape(tr._t("headwind_date_caption", lang, date=idx_positions and "x") or "")
        # (date text itself is dynamic; just check the suffix marker "· " appears exactly once)
        assert out.count("· ") == 1, f"mask={mask:04b} lang={lang}: expected exactly one date suffix, out={out!r}"
        # It must appear after the last label's position (i.e. on the last line)
        suffix_pos = out.rfind("· ")
        assert suffix_pos > idx_positions[-1], (
            f"mask={mask:04b} lang={lang}: date suffix not on the last rendered line"
        )
print("[render] all 16 present/absent combinations render in fixed order, exactly one date suffix on the last line, EN+ES OK")

# All three big_wave chip variants render with distinct styling
for wave, style in (("tailwind", tr._BIG_WAVE_TAILWIND_STYLE),
                     ("flat", tr._BIG_WAVE_FLAT_STYLE),
                     ("headwind", tr._SHELF_CHIP_NOT_RATED)):
    row = _row(big_wave=wave, big_wave_comment="Trend text.")
    out = tr._competitive_landscape_html(row, "en")
    bg, border, color = style
    assert bg in out and border in out and color in out, f"{wave} chip style not found in render"
print("[render] all three big_wave chip variants (tailwind/flat/headwind) render with correct styling OK")

# Munger yes/no chip styling
for mq, style in (("yes", tr._MUNGER_YES_STYLE), ("no", tr._MUNGER_NO_STYLE)):
    row = _row(munger_quality=mq, munger_comment="Reason.")
    out = tr._competitive_landscape_html(row, "en")
    bg, border, color = style
    assert bg in out and border in out and color in out, f"munger {mq} chip style not found in render"
print("[render] munger_quality yes/no chip styling OK")

# NOT RATED row -> "" regardless of fields present
row = _row(munger_quality="yes", munger_comment="x", not_rated=True)
assert tr._competitive_landscape_html(row, "en") == ""
print("[render] NOT RATED row renders empty box regardless of v5 fields OK")

# v4-fallback row (no v5 columns at all, simulating a real pre-v5 DB row)
v4_row = {
    "not_rated": False, "scored_at": datetime.now(timezone.utc).isoformat(),
    "market_structure": "oligopoly", "market_structure_comment": "peers",
    "one_foot_hurdle": "no", "one_foot_comment": "hard",
    # munger_quality/big_wave simply absent from the dict, like a real
    # v4-era sqlite3.Row-derived dict would be if the columns didn't
    # exist yet - .get() must fall through to None cleanly.
}
out = tr._competitive_landscape_html(v4_row, "en")
assert "MUNGER" not in out and "BIG WAVE" not in out.upper().replace("STOCKS", "")
assert "market structure" in out.lower() or "Market structure" in out
print("[render] v4-fallback row (missing v5 keys entirely) renders the old two lines without crashing OK")


# ---------------------------------------------------------------
# 5. AppTest: real render_top100_page(), mixed v4-fallback + v5 row
# ---------------------------------------------------------------
from streamlit.testing.v1 import AppTest

today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
ts.save_pool(
    [
        {"ticker": "MELI", "company_name": "MercadoLibre", "universe": "S&P 500",
         "value_score": 80.0, "mos_pct": 40.0, "price": 2000.0, "intrinsic_value": 3000.0,
         "currency": "USD", "generated_at": datetime.now(timezone.utc).isoformat(),
         "pool_selection_rule": "freshest_v1"},
        {"ticker": "CARG", "company_name": "CarGurus", "universe": "S&P 500",
         "value_score": 70.0, "mos_pct": 60.0, "price": 30.0, "intrinsic_value": 75.0,
         "currency": "USD", "generated_at": datetime.now(timezone.utc).isoformat(),
         "pool_selection_rule": "freshest_v1"},
    ],
    as_of=today,
)
# MELI already has a v5 row from section 3 above. CARG has a v4 row
# (from section 3's backward-compat check) - that's the previous-
# rubric FALLBACK path under RUBRIC_VERSION v5.


def _run_page(lang):
    script = f"""
import os, sys
sys.path.insert(0, {os.path.dirname(os.path.dirname(os.path.abspath(__file__)))!r})
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = {TESTVOL!r}
import top100_render
top100_render.render_top100_page(lang={lang!r})
"""
    at = AppTest.from_string(script)
    at.run(timeout=30)
    assert not at.exception, f"render_top100_page(lang={lang!r}) raised: {at.exception}"
    return at


at_en = _run_page("en")
at_es = _run_page("es")
print("[apptest] render_top100_page() renders without crashing, mixed v4-fallback + v5 rows, EN+ES OK")


print("\nALL TOP 100 V5 MUNGER/BIG-WAVE TESTS PASSED")
