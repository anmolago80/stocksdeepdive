"""
top100_render.py

Rendering for the Top 100 page - app.py's page_top100() is a thin
wrapper that calls render_top100_page() here, same split compounder_ui.
py already established for the Trading Cost tab (a *_render.py/*_ui.py
module owns markup, app.py owns the page function/owner gate/nav
wiring). Reads top100_store.py directly (no Streamlit dependency in
that module) and top100_engine.py for the pure composite/changes math.
"""

import datetime as _dt
import html
import os

import streamlit as st

import compounder_ui
import i18n
import quote_snapshot_store
import top100_engine
import top100_store
import trading_cost_engine


def _truthy(v):
    return (v or "").strip().lower() in ("1", "true", "yes", "on")


# Commit 3 rollout flag - same {"1","true","yes","on"} truthy convention
# and same "module-level flag on the feature's own *_render.py/*_ui.py
# module" placement as compounder_ui.TRADING_COST_ENABLED, but the
# SEMANTICS are inverted from that precedent: TRADING_COST_ENABLED
# unset/false means the tab doesn't exist at all for anyone, whereas
# TOP100_PUBLIC unset/false means the page exists but is owner-only
# (page_top100() in app.py checks ai_gate.is_owner() when this is
# False - same independent, page-function-level check page_admin_
# dashboard() itself uses) so the owner can review several nights of
# real scores before flipping this to true and opening the page to
# everyone. Default false is deliberate per the task's own instruction.
TOP100_PUBLIC = _truthy(os.environ.get("TOP100_PUBLIC"))

# Tradability chip threshold (task's own words: "any name whose latest
# recorded spread exceeds 1%") - a DIFFERENT number from trading_cost_
# engine's own TIGHT/WIDE thresholds (0.5/1.5) and now_spread_
# classification's own (0.5/2.0) - deliberately a third, independent
# scale for a third, independent purpose (this is a "is this name even
# worth trying to trade" flag on a stock-QUALITY page, not a spread-
# classification widget), so intentionally not reusing either constant.
TRADABILITY_SPREAD_THRESHOLD_PCT = 1.0

_SCORE_BAND_COLOR = {1: "red", 2: "red", 3: "amber", 4: "green", 5: "green"}

# v2 amendment: sentiment 3-way collapse of the site's own real per-
# ticker "Psychology" number (nightly_scan.py's fear-greed-fomo score,
# now carried onto the pool row - see top100_engine.select_top100_
# pool()'s own comment). Boundaries are the SAME ones deep_dive_engine.
# analyze()'s own 5-state classification already uses (>20 FEARFUL,
# 5..20 CALM, -5..5 NEUTRAL, -20..-5 GREEDY, <-20 OVERHEATED) merged
# down to the task's own requested 3 states: CALM and NEUTRAL both
# read as "neutral" here, GREEDY and OVERHEATED both read as "greedy" -
# deep_dive_engine.py itself is not imported (a heavy module with its
# own side effects at import time in some paths - not worth the
# coupling for three comparisons against a number already on the row).
_SENTIMENT_FEARFUL_MIN = 20
_SENTIMENT_GREEDY_MAX = -5


def _sentiment_bucket(psychology):
    """None if there's no Psychology reading at all (never guessed);
    otherwise "fearful"/"neutral"/"greedy"."""
    if psychology is None:
        return None
    if psychology > _SENTIMENT_FEARFUL_MIN:
        return "fearful"
    if psychology < _SENTIMENT_GREEDY_MAX:
        return "greedy"
    return "neutral"


_SENTIMENT_COLOR = {"fearful": "blue", "neutral": None, "greedy": "amber"}
_SEVERITY_ALERT_MIN = 4


def _t(key, lang, **fmt):
    return i18n.t(f"top100.{key}", lang, **fmt)


# -----------------------------------------------------------------
# Top 100 Ranking Rework (25 Sep 2026, owner-approved mock, "top100_
# research_ranking_mock_2.html"): the four-way sort bar above the
# tabs. All display-layer - reads the same "composite"/"score_row"
# _enriched_pool() already computes, never re-derives a score.
# -----------------------------------------------------------------

SORT_RESEARCH = "research"
SORT_VALUE_TODAY = "value_today"
SORT_VALUE_SCORE = "value_score"
SORT_SEVERITY = "severity"

# Order matches the task's own a/b/c/d listing - also the sort bar's
# own left-to-right option order and the methodology panel's own
# per-view explanation order.
SORT_MODES = [SORT_RESEARCH, SORT_VALUE_TODAY, SORT_VALUE_SCORE, SORT_SEVERITY]

# "Best value today" gate (task's own words: "Research Score >= 70") -
# rows below this are not reordered, they are not shown in that view
# at all.
VALUE_TODAY_GATE = 70

# Sentinel sort keys for a missing numeric field, so a tie-break or a
# primary sort key never crashes on None - always sorts LAST among its
# peers (a missing MOS/severity is never treated as "best").
_MISSING_MOS_KEY = -1_000_000
_MISSING_SEVERITY_KEY = -1


def _key_research(row):
    """Point 2a: Research Score desc, ties by higher MOS, then ticker
    A-Z. Only ever called on rated rows (composite is not None)."""
    mos = row.get("mos_pct")
    return (-row["composite"], -(mos if mos is not None else _MISSING_MOS_KEY), row["ticker"])


def _key_value_today(row):
    """Point 2b: live MOS desc (the gate itself is applied by the
    caller before sorting) - ticker A-Z breaks a tie."""
    mos = row.get("mos_pct")
    return (-(mos if mos is not None else _MISSING_MOS_KEY), row["ticker"])


def _key_value_score(row):
    """Point 2c: the site's own numeric ordering, unchanged semantics -
    Value Score desc, ticker A-Z breaks a tie (the tie-break itself is
    new, purely for a deterministic render; the ordering it breaks
    ties within is untouched)."""
    return (-row["value_score"], row["ticker"])


def _key_severity(row):
    """Point 2d: inversion severity desc (5 first), sub-sorted by
    Research Score desc within each severity band. A rated row that
    somehow has no inversion severity (the model can, in principle,
    return one even when not NOT RATED) sorts after severity 1, never
    treated as the worst-case top of the list."""
    score_row = row.get("score_row") or {}
    severity = score_row.get("inversion_severity")
    return (-(severity if severity is not None else _MISSING_SEVERITY_KEY), -row["composite"], row["ticker"])


def _sort_and_gate_rated(rows, sort_mode):
    """Every RATED row (composite is not None), gated and sorted per
    the active sort mode. Point 3's bottom-shelf rows (composite is
    None) are never returned here - callers add the shelf separately
    (Full 100 tab only; the Top 20 tabs never show it at all, per the
    task's own "excluded from Top-20 tabs entirely" instruction).

    Interleave choice for point 3's "under the Value Score view they
    may instead interleave normally... implementer's choice" clause:
    this codebase keeps unrated companies shelved under EVERY sort
    view, Value Score included - one consistent code path rather than
    a special case, reported as such in this task's own commit
    message."""
    rated = [r for r in rows if r["composite"] is not None]
    if sort_mode == SORT_VALUE_TODAY:
        rated = [r for r in rated if r["composite"] >= VALUE_TODAY_GATE]
        return sorted(rated, key=_key_value_today)
    if sort_mode == SORT_VALUE_SCORE:
        return sorted(rated, key=_key_value_score)
    if sort_mode == SORT_SEVERITY:
        return sorted(rated, key=_key_severity)
    return sorted(rated, key=_key_research)


def _leading_metric(row, sort_mode, lang):
    """(label, value_text) for the row header's leading, sort-defining
    number - the mock's own "the leading number on each row is the one
    doing the sorting" rule."""
    if sort_mode == SORT_VALUE_TODAY:
        mos = row.get("mos_pct")
        return _t("col_mos", lang), (f"{mos:.1f}%" if mos is not None else "—")
    if sort_mode == SORT_VALUE_SCORE:
        return _t("col_value_score", lang), f"{row['value_score']:.1f}"
    if sort_mode == SORT_SEVERITY:
        score_row = row.get("score_row") or {}
        severity = score_row.get("inversion_severity")
        return _t("col_severity", lang), (f"{severity}/5" if severity is not None else "—")
    return _t("col_research_score", lang), f"{row['composite']:.1f}"


def _leading_metric_html(row, sort_mode, lang):
    label, value = _leading_metric(row, sort_mode, lang)
    return (
        "<span style='margin-left:auto;font-size:14px;color:#34d399;font-weight:800;'>"
        f"{html.escape(label)} {html.escape(value)}</span>"
    )


def _secondary_metrics_html(row, sort_mode, lang):
    """The other stat(s) next to the leading number - whichever of
    Value Score / MOS / Research Score ISN'T already the leading
    metric for this sort mode, same "MOS · Value Score" / "Research ·
    Value Score" pairing the mock shows."""
    parts = []
    if sort_mode != SORT_VALUE_SCORE:
        parts.append(f"{html.escape(_t('col_value_score', lang))}: <b style='color:#e6edf5;'>{row['value_score']:.1f}</b>")
    if sort_mode != SORT_VALUE_TODAY and row.get("mos_pct") is not None:
        parts.append(f"{html.escape(_t('col_mos', lang))}: <b style='color:#e6edf5;'>{row['mos_pct']:.1f}%</b>")
    if sort_mode != SORT_RESEARCH:
        parts.append(f"{html.escape(_t('col_research_score', lang))}: <b style='color:#e6edf5;'>{row['composite']:.1f}</b>")
    if not parts:
        return ""
    return f"<span style='color:#8aa0b8;font-size:12px;'>{' · '.join(parts)}</span>"


def _dimension_tooltip_text(key, dim, lang):
    """The hover title= text for one dimension chip (task's own "full
    name, anchor meaning, justification with source period" - native
    HTML title attribute, same mechanism app.py's own earnings-word
    track chip already uses for a hover tooltip on custom HTML, no JS
    needed). Also the ONE place the tap-expand detail below reuses for
    its own per-dimension heading, so the two surfaces never drift."""
    label = _t(f"dim_{key}", lang)
    anchor = _t(f"dim_anchor_{key}", lang)
    lines = [label, anchor]
    if dim:
        score = dim.get("score")
        just = dim.get("justification")
        period = dim.get("source_period")
        if just:
            score_text = str(score) if score is not None else "—"
            period_text = f" ({period})" if period else ""
            lines.append(f"{_t('col_your_score', lang)}: {score_text}{period_text} — {just}")
    return "\n".join(lines)


def _score_chip_html(key, score, dim, lang):
    """One dimension chip - the score number colour-banded (1-2 red,
    3 amber, 4-5 green - the task's own "colour-banded per the mock"),
    a muted grey "—" chip when the dimension itself is null (never a
    fabricated colour for a reading that doesn't exist), with a native
    hover tooltip (title=) carrying the full name, the anchor meaning,
    and this company's own justification + source period - "no
    justification may be unreachable" is also satisfied on phones (no
    hover) by the row's own tap-expand detail below, which shows the
    exact same text."""
    label = _t(f"dim_{key}", lang)
    if score is None:
        color, bg, text = "#5b7290", "#1a2332", "—"
    else:
        band = _SCORE_BAND_COLOR[score]
        color = compounder_ui._CP_COLOR_TEXT[band]
        bg = compounder_ui._CP_COLOR_FILL[band]
        text = str(score)
    tooltip = html.escape(_dimension_tooltip_text(key, dim, lang))
    return (
        "<span title='" + tooltip + "' "
        "style='display:inline-flex;align-items:center;gap:4px;border-radius:8px;"
        f"padding:3px 8px;margin:2px 4px 2px 0;font-size:11px;background:{bg};color:{color};"
        "font-weight:700;cursor:help;'>"
        f"{html.escape(label)} <span style='font-family:ui-monospace,Menlo,SFMono-Regular,monospace;'>"
        f"{html.escape(text)}</span></span>"
    )


def _sentiment_chip_html(psychology, lang):
    """Free display chip (task's own words: "never scored") - the
    site's own real Psychology reading collapsed to fearful/neutral/
    greedy. Neutral gets no colour tint at all (the site's own visual
    convention for "nothing notable" - see _CP_COLOR_TEXT's own
    red/amber/green/blue vocabulary, which has no neutral entry by
    design); fearful/greedy reuse the existing blue/amber tokens."""
    bucket = _sentiment_bucket(psychology)
    if bucket is None:
        return ""
    band = _SENTIMENT_COLOR[bucket]
    if band:
        color = compounder_ui._CP_COLOR_TEXT[band]
        bg = compounder_ui._CP_COLOR_FILL[band]
        border = f"border:1px solid {color};"
    else:
        color, bg, border = "#8aa0b8", "#141d30", "border:1px solid #263654;"
    return (
        "<span style='display:inline-flex;align-items:center;border-radius:999px;padding:2px 9px;"
        f"margin-left:6px;font-size:11px;font-weight:700;background:{bg};{border}color:{color};'>"
        f"{html.escape(_t(f'sentiment_{bucket}', lang))}</span>"
    )


def _tradability_chip_html(spread_pct, lang):
    return (
        "<span style='display:inline-flex;align-items:center;gap:4px;border-radius:999px;"
        "padding:2px 9px;margin-left:6px;font-size:11px;font-weight:700;background:#2d1420;"
        f"border:1px solid #7f1d3a;color:{compounder_ui._CP_COLOR_TEXT['red']};'>"
        f"{html.escape(_t('tradability_chip', lang, pct=f'{spread_pct:.1f}'))}</span>"
    )


def _shelf_chip_html(text, bg, border, color):
    """One bottom-shelf chip (point 3) - two distinct variants, exact
    hex values from the owner-approved mock's own .chipn (AWAITING)
    and .chipw (NOT RATED) classes."""
    return (
        "<span style='display:inline-flex;align-items:center;border-radius:999px;padding:1px 9px;"
        f"font-size:10.5px;font-weight:700;background:{bg};border:1px solid {border};color:{color};'>"
        f"{html.escape(text)}</span>"
    )


def _finer_industry_by_ticker():
    """{ticker: industry_string} from compounder_data.json's own hand-
    researched coverage (build_compounder_data.py) - Andrew's finer-
    grained industry classification where he's covered a ticker, read
    the SAME way research_snapshot_render._load_research_data() and
    app.py's own coverage-industry lookups (e.g. app.py's Research page
    opener card) already do: a cached JSON file on the Railway Volume,
    no live fetch, direct ticker-string lookup with no case-forcing
    (matching every existing caller of this data). {} when the file
    doesn't exist yet (a fresh volume before the first admin rebuild)
    or is malformed - never raises, a ticker simply gets no finer-
    industry refinement rather than an error."""
    try:
        import research_snapshot_render
        data = research_snapshot_render._load_research_data()
        tickers = (data or {}).get("tickers") or {}
        return {t: (info or {}).get("industry") for t, info in tickers.items()}
    except Exception:
        return {}


def _industry_tag_html(sector, finer_industry):
    """Top 100 Commit 5 (25 Sep 2026, owner-reported, industry mock):
    the small uppercase muted-slate industry tag - `sector` from the
    pool row (top100_engine.select_top100_pool()'s own "Sector" read,
    ultimately the nightly scan's cached value), refined with `finer_
    industry` (compounder_data.json's own hand-researched value, or
    None where Andrew hasn't covered this ticker) when both are known
    and actually differ. Cached data only - no live fetch, and never
    enters composite_score() or any dimension - display-only, same
    status as the sentiment/tradability chips. Returns "" (renders
    nothing) when NEITHER is known, per the task's own explicit "a
    ticker with no sector on file simply shows no tag" instruction."""
    sector = (sector or "").strip()
    finer_industry = (finer_industry or "").strip()
    if finer_industry and sector and finer_industry.lower() != sector.lower():
        label = f"{sector} — {finer_industry}"
    elif finer_industry:
        label = finer_industry
    elif sector:
        label = sector
    else:
        return ""
    return (
        "<span style='display:inline-block;background:#0e1930;border:1px solid #2a3f63;"
        "color:#7f95b3;border-radius:6px;padding:1px 9px;font-size:10.5px;"
        "letter-spacing:.05em;text-transform:uppercase;font-weight:600;'>"
        f"{html.escape(label)}</span>"
    )


# -----------------------------------------------------------------
# Top 20 Australia guaranteed-twenty (25 Sep 2026, owner-approved
# mock, "top20_australia_extended_mock.html") - origin badges. Exact
# colour tokens from the mock's own .src.pool (teal) and .src.ext
# (violet) classes.
# -----------------------------------------------------------------

def _global_value_score_ranks(pool):
    """{ticker: 1-based rank} by Value Score descending across the
    FULL global pool (all 100, currency-agnostic) - the number the
    teal "TOP 100 · #<n>" badge shows (point 3's own "global rank
    #<n>" wording), independent of whatever sort view the Australia
    tab itself is currently using."""
    ranked = sorted(pool, key=lambda r: (-(r["value_score"] or 0), r["ticker"]))
    return {r["ticker"]: i for i, r in enumerate(ranked, start=1)}


def _origin_pool_badge_html(rank, lang):
    tooltip = html.escape(_t("origin_pool_tooltip", lang, rank=rank))
    label = html.escape(_t("origin_pool_badge", lang, rank=rank))
    return (
        f"<span title='{tooltip}' style='display:inline-flex;align-items:center;gap:5px;"
        "border-radius:999px;padding:1px 10px;font-size:10.5px;font-weight:700;cursor:default;"
        "background:#0e2a26;border:1px solid #14532d;color:#34d399;'>"
        f"{label}</span>"
    )


def _origin_extension_badge_html(lang):
    tooltip = html.escape(_t("origin_extension_tooltip", lang))
    label = html.escape(_t("origin_extension_badge", lang))
    return (
        f"<span title='{tooltip}' style='display:inline-flex;align-items:center;gap:5px;"
        "border-radius:999px;padding:1px 10px;font-size:10.5px;font-weight:700;cursor:default;"
        "background:#1d1530;border:1px solid #5b3fa8;color:#b7a3f0;'>"
        f"{label}</span>"
    )


def _tradability_spread_pct(ticker):
    """None if there's no recorded snapshot for `ticker`, or its
    latest recorded spread% otherwise - display-flag only (the task's
    own words: "read from quote_snapshot_store — display flag only"),
    never fed back into the composite or any other computed score."""
    latest = quote_snapshot_store.latest_snapshot(ticker)
    if not latest or latest.get("bid") is None or latest.get("ask") is None:
        return None
    return trading_cost_engine._recorded_spread_pct(latest["bid"], latest["ask"])


def _inversion_line_html(score_row, lang):
    """"⚠ Inversion: {scenario} — severity {n}/5" (task's own exact
    wording) - severities 1-3 muted grey, 4-5 in the site's red alert
    styling (_CP_COLOR_TEXT/_CP_COLOR_FILL['red'], same tokens the
    tradability chip already uses). "" for a NOT RATED row or one with
    no inversion data at all - "nothing invented" (top100_engine's own
    server-side null-enforcement is the real guarantee; this is a
    second, display-layer check on top of it)."""
    if not score_row or score_row.get("not_rated"):
        return ""
    scenario = score_row.get("inversion_scenario")
    severity = score_row.get("inversion_severity")
    if not scenario or severity is None:
        return ""
    text = html.escape(_t("inversion_line", lang, scenario=scenario, severity=severity))
    if severity >= _SEVERITY_ALERT_MIN:
        color, bg = compounder_ui._CP_COLOR_TEXT["red"], compounder_ui._CP_COLOR_FILL["red"]
        style = f"color:{color};background:{bg};border:1px solid {color};"
    else:
        style = "color:#8aa0b8;background:transparent;border:1px solid #263654;"
    return (
        f"<div style='{style}border-radius:8px;padding:6px 10px;margin-top:8px;font-size:12px;'>"
        f"{text}</div>"
    )


def _row_edge_accent_style(score_row):
    """The mock's own "red row-edge accent" for a severity 4-5
    inversion (XRO/OCL rows) - a coloured left border on the row card
    itself, so the alert is visible without opening anything."""
    if not score_row or score_row.get("not_rated"):
        return ""
    severity = score_row.get("inversion_severity")
    if severity is not None and severity >= _SEVERITY_ALERT_MIN:
        return f"border-left:4px solid {compounder_ui._CP_COLOR_TEXT['red']};"
    return ""


def _render_row(rank, row, lang, finer_industry, sort_mode, origin_badge_html=None):
    """One RATED company row - rank/ticker(linked), the leading number
    for the active sort (task's own "the leading number on each row is
    the one doing the sorting"), the other stats next to it, the ten
    dimension chips (each with a hover tooltip), free sentiment/
    tradability chips, the inversion line (severity-styled), and a
    row-level details toggle that expands every one of the ten
    justifications stacked - the SAME text the hover tooltips carry,
    so a phone (no hover) or a "read everything" desktop visit never
    leaves a justification unreachable.

    PRECONDITION (Top 100 Ranking Rework): only ever called for a
    RATED row - composite is not None, score_row is not None and not
    not_rated. Both callers (_render_top20_tab, _render_full100_tab)
    already filter to that via _sort_and_gate_rated() before reaching
    here; an unrated company lives in the bottom shelf instead
    (_shelf_row_html), which has no dimension chips or Research Score
    to show.

    `finer_industry` (Top 100 Commit 5, 25 Sep 2026, owner-reported,
    industry mock): this ticker's compounder_data.json industry string
    (or None), looked up ONCE per page render by the caller (_finer_
    industry_by_ticker()) rather than re-reading that file per row -
    combined with row["sector"] into the industry tag right after the
    company name, present on normal and red-alert rows alike.

    `origin_badge_html` (Top 20 Australia guaranteed-twenty, 25 Sep
    2026, owner-approved mock): the teal "TOP 100 · #<n>" / violet
    "ASX EXTENSION" badge HTML (or None - every other tab passes
    nothing, unaffected) - Top 20 Australia's own caller (_render_
    top20_australia_tab()) computes this fresh per row, right after
    the industry tag, matching the mock's own DOM order."""
    ticker = row["ticker"]
    score_row = row["score_row"]

    header_bits = [
        f"<span style='color:#5b7290;font-family:ui-monospace,Menlo,SFMono-Regular,monospace;"
        f"font-size:12px;'>#{rank}</span>",
        f"<a href='/deep-dive?ticker={html.escape(ticker)}' target='_self' "
        "style='color:#5ed3f0;font-weight:700;text-decoration:none;font-size:14px;'>"
        f"{html.escape(ticker)}</a>",
    ]
    if row.get("company_name"):
        header_bits.append(f"<span style='color:#8aa0b8;font-size:12px;'>{html.escape(row['company_name'])}</span>")
    industry_tag = _industry_tag_html(row.get("sector"), finer_industry)
    if industry_tag:
        header_bits.append(industry_tag)
    if origin_badge_html:
        header_bits.append(origin_badge_html)
    header_bits.append(_leading_metric_html(row, sort_mode, lang))
    secondary_html = _secondary_metrics_html(row, sort_mode, lang)
    if secondary_html:
        header_bits.append(secondary_html)

    spread_pct = _tradability_spread_pct(ticker)
    tradable_chip = ""
    if spread_pct is not None and spread_pct > TRADABILITY_SPREAD_THRESHOLD_PCT:
        tradable_chip = _tradability_chip_html(spread_pct, lang)
    sentiment_chip = _sentiment_chip_html(row.get("psychology"), lang)

    st.markdown(
        f"<div style='background:#0b1526;border:1px solid #1a2b4a;{_row_edge_accent_style(score_row)}"
        "border-radius:10px;padding:10px 14px;margin-bottom:8px;'>"
        "<div style='display:flex;flex-wrap:wrap;align-items:center;gap:12px;'>"
        + "".join(header_bits) + tradable_chip + sentiment_chip +
        "</div>"
        "<div style='margin-top:8px;'>"
        + "".join(
            _score_chip_html(key, (score_row["dims"].get(key) or {}).get("score"),
                              score_row["dims"].get(key), lang)
            for key in top100_engine.DIMENSION_KEYS
        )
        + "</div>"
        + _inversion_line_html(score_row, lang) +
        "</div>",
        unsafe_allow_html=True,
    )

    # Row-level details toggle (task's own words) - the tap-expand path
    # for phones (no hover) AND the desktop "read everything" path.
    with st.expander(f"{ticker} — {_t('why_here_label', lang)}", expanded=False):
        for key in top100_engine.DIMENSION_KEYS:
            dim = score_row["dims"].get(key) or {}
            if dim.get("score") is None and not dim.get("justification"):
                continue
            score_text = str(dim["score"]) if dim.get("score") is not None else "—"
            period = f" ({dim['source_period']})" if dim.get("source_period") else ""
            st.markdown(
                f"**{html.escape(_t(f'dim_{key}', lang))} — {html.escape(score_text)}**"
                f"{html.escape(period)}  \n{html.escape(dim.get('justification') or '')}"
            )


def _enriched_pool():
    """current_pool() rows, each augmented with "score_row" (top100_
    store.get_score() for the current quarter/model, or None) and
    "composite" (top100_engine.composite_score(), or None) - the one
    place every tab reads from, so the Top 20 tabs and the Full 100
    tab can never compute composite differently from each other."""
    pool = top100_store.current_pool()
    quarter = top100_engine.current_quarter()
    scores = top100_store.scores_for_quarter_model(
        quarter, top100_engine.MODEL_TOP100, top100_engine.RUBRIC_VERSION)
    out = []
    for row in pool:
        score_row = scores.get(row["ticker"])
        composite = top100_engine.composite_score(score_row)
        out.append({**row, "score_row": score_row, "composite": composite})
    return out


def _enriched_asx_extension():
    """top100_store.current_asx_extension() rows, augmented with score_
    row/composite exactly like _enriched_pool() does for the global
    pool - Top 20 Australia's own guaranteed-twenty supplement. NEVER
    read by any other tab, the homepage teaser, or the changes strip -
    they all read _enriched_pool() (i.e. top100_store.current_pool())
    alone, which never includes an extension row."""
    extension = top100_store.current_asx_extension()
    quarter = top100_engine.current_quarter()
    scores = top100_store.scores_for_quarter_model(
        quarter, top100_engine.MODEL_TOP100, top100_engine.RUBRIC_VERSION)
    out = []
    for row in extension:
        score_row = scores.get(row["ticker"])
        composite = top100_engine.composite_score(score_row)
        out.append({**row, "score_row": score_row, "composite": composite})
    return out


def _render_top20_tab(enriched, lang, finer_industry, sort_mode, currency=None):
    """Top 20 by the active sort (point 2, "applying to every tab
    including Full 100"). Point 3: unrated companies are excluded from
    the Top 20 tabs entirely - never shown here, not even in a shelf -
    which falls straight out of _sort_and_gate_rated() only ever
    returning rated rows."""
    rows = _sort_and_gate_rated(enriched, sort_mode)
    if currency:
        rows = [r for r in rows if r["currency"] == currency]
    rows = rows[:20]
    if not rows:
        st.caption(_t("empty_tab", lang))
        return
    for i, row in enumerate(rows, start=1):
        _render_row(i, row, lang, finer_industry.get(row["ticker"]), sort_mode)


def _render_full100_tab(enriched, lang, finer_industry, sort_mode):
    """Full 100: the rated companies under the active sort, then
    (point 3) a labelled bottom shelf of unrated companies ordered by
    Value Score among themselves - shown under every sort view except
    "Best value today" (the >=70 gate excludes an unrated company from
    that view exactly as it excludes any rated company that misses the
    cut, so a company with no Research Score at all has nothing to
    show there either)."""
    rows = _sort_and_gate_rated(enriched, sort_mode)
    if not rows:
        st.caption(_t("empty_tab", lang))
    else:
        for i, row in enumerate(rows, start=1):
            _render_row(i, row, lang, finer_industry.get(row["ticker"]), sort_mode)

    if sort_mode != SORT_VALUE_TODAY:
        shelf_rows = sorted(
            (r for r in enriched if r["composite"] is None),
            key=_key_value_score,
        )
        _render_bottom_shelf(shelf_rows, lang)


def _render_top20_australia_tab(enriched, lang, finer_industry, sort_mode):
    """Top 20 · Australia guaranteed-twenty (25 Sep 2026, owner-
    approved mock, "top20_australia_extended_mock.html") - a GUARANTEED
    twenty: every AUD pool row plus (point 1) the ASX extension's own
    next-best-by-Value-Score Australians. Ranked by the active sort +
    bottom-shelf rules exactly like every other tab (point 3), with an
    origin badge on every row: teal "TOP 100 · #<n>" (point 3, re-
    derived HERE from the live global pool's own Value Score ordering -
    never the stored asx_extension flag - so a company graduating
    into/out of the global 100 flips the badge automatically) or
    violet "ASX EXTENSION". The global tabs (Mixed/USA/Full 100), the
    homepage teaser and the changes strip are all UNTOUCHED - they
    read `enriched` (i.e. _enriched_pool()) alone, which never
    contains an extension row; this function is the only one that also
    reads _enriched_asx_extension()."""
    pool_au = [r for r in enriched if r["currency"] == "AUD"]
    extension = _enriched_asx_extension()
    combined = pool_au + extension

    global_pool_tickers = {r["ticker"] for r in enriched}
    global_ranks = _global_value_score_ranks(enriched)

    def origin_badge(row):
        ticker = row["ticker"]
        if ticker in global_pool_tickers:
            return _origin_pool_badge_html(global_ranks[ticker], lang)
        return _origin_extension_badge_html(lang)

    rows = _sort_and_gate_rated(combined, sort_mode)
    if not rows:
        st.caption(_t("empty_tab", lang))
    else:
        for i, row in enumerate(rows, start=1):
            _render_row(i, row, lang, finer_industry.get(row["ticker"]), sort_mode,
                        origin_badge_html=origin_badge(row))

    if sort_mode != SORT_VALUE_TODAY:
        shelf_rows = sorted(
            (r for r in combined if r["composite"] is None),
            key=_key_value_score,
        )
        _render_bottom_shelf(shelf_rows, lang, origin_badge_fn=origin_badge)


# Bottom-shelf chip colours (point 3) - exact hex values from the mock's
# own .chipn (AWAITING - neutral grey-blue) and .chipw (NOT RATED -
# amber "insufficient record" warning) classes.
_SHELF_CHIP_AWAITING = ("#1a2333", "#2a3f63", "#7f95b3")
_SHELF_CHIP_NOT_RATED = ("#2a2413", "#7c5e10", "#fbbf24")


def _shelf_row_html(row, lang, origin_badge_html=None):
    """One bottom-shelf row (point 3) - a compact chip+ticker+stats
    line, never the full dimension-chip card _render_row() draws
    (there are no dimensions to show). Two distinct chip variants:
    "⏳ AWAITING" when score_row is None entirely (never submitted/
    ingested - a genuine newcomer), "◇ NOT RATED" when score_row
    exists but the model declined to score it (>= NOT_RATED_MIN_NULLS
    null dimensions) - the task's own explicit distinction.
    `origin_badge_html` (Top 20 Australia guaranteed-twenty): None on
    every other tab; Australia's own caller passes its badge so even
    an unrated extension member is clearly marked."""
    score_row = row.get("score_row")
    if score_row is None:
        chip = _shelf_chip_html(_t("shelf_chip_awaiting", lang), *_SHELF_CHIP_AWAITING)
        caption = _t("shelf_caption_awaiting", lang)
    else:
        chip = _shelf_chip_html(_t("shelf_chip_not_rated", lang), *_SHELF_CHIP_NOT_RATED)
        caption = _t("shelf_caption_not_rated", lang)
    ticker = row["ticker"]
    badge_html = origin_badge_html or ""
    mos_html = ""
    if row.get("mos_pct") is not None:
        mos_html = (
            f" · {html.escape(_t('col_mos', lang))}: "
            f"<b style='color:#e6edf5;'>{row['mos_pct']:.1f}%</b>"
        )
    return (
        "<div style='display:flex;gap:10px;align-items:baseline;font-size:12.5px;"
        "padding:4px 0;color:#8aa0b8;flex-wrap:wrap;'>"
        + chip +
        f"<a href='/deep-dive?ticker={html.escape(ticker)}' target='_self' "
        "style='color:#2dd4bf;font-weight:800;font-size:13px;text-decoration:none;'>"
        f"{html.escape(ticker)}</a>"
        + badge_html +
        f"<span>{html.escape(_t('col_value_score', lang))}: "
        f"<b style='color:#e6edf5;'>{row['value_score']:.1f}</b>{mos_html}</span>"
        f"<span style='color:#5b7290;font-size:11px;'>{html.escape(caption)}</span>"
        "</div>"
    )


def _render_bottom_shelf(shelf_rows, lang, origin_badge_fn=None):
    """Full 100's (and Australia's own) labelled bottom-shelf section
    (point 3) - companies with no Research Score can't be interleaved
    with rated ones, so they sit here, ordered by Value Score among
    themselves (already the caller's own sort key). Renders nothing
    when there's nothing to shelve. `origin_badge_fn` (Top 20 Australia
    guaranteed-twenty): None on Full 100; Australia's own caller passes
    a row->badge-html function."""
    if not shelf_rows:
        return
    rows_html = "".join(
        _shelf_row_html(r, lang, origin_badge_html=(origin_badge_fn(r) if origin_badge_fn else None))
        for r in shelf_rows
    )
    st.markdown(
        "<div style='border:1px dashed #2a3f63;border-radius:12px;padding:12px 16px;margin-top:16px;'>"
        "<h3 style='margin:0 0 8px;font-size:12px;color:#8aa0b8;letter-spacing:.05em;"
        f"text-transform:uppercase;'>{html.escape(_t('shelf_heading', lang))}</h3>"
        + rows_html +
        "</div>",
        unsafe_allow_html=True,
    )


# Top 100 Commit 5 (25 Sep 2026, owner-reported, industry mock): a
# dropped ticker's "reason" (top100_engine.pool_changes()'s own two
# fixed strings - NOT changed by this commit, display-only) mapped to
# an i18n key so the compact strip's tooltip is genuinely localized in
# Spanish, rather than interpolating the raw English reason string
# into an otherwise-translated sentence (the old bullet-list's own
# behaviour). An unrecognized reason (pool_changes() wording changed
# without this map being updated) falls back to the raw string rather
# than crashing or showing a blank tooltip.
_DROPPED_REASON_I18N_KEYS = {
    "still scanned, but its Value Score fell outside the top 100": "changes_strip_reason_cutoff",
    "no longer appears in any scanned universe": "changes_strip_reason_delisted",
}


def _dropped_reason_text(reason, lang):
    key = _DROPPED_REASON_I18N_KEYS.get(reason)
    return _t(key, lang) if key else reason


def _changes_chip_html(text, variant, tooltip=None):
    """One ticker chip for the compact changes strip - variant is
    "new"/"drop"/"wait", exact color tokens from the owner-approved
    mock (top100_changes_industry_mock.html)."""
    colors = {
        "new": ("#10312d", "#14532d", "#34d399"),
        "drop": ("#101a2e", "#1f3352", "#8aa0b8"),
        "wait": ("#2a2413", "#7c5e10", "#fbbf24"),
    }
    bg, border, color = colors[variant]
    title_attr = f" title='{html.escape(tooltip)}'" if tooltip else ""
    return (
        "<span style='display:inline-block;border-radius:999px;padding:2px 10px;"
        f"font-size:11.5px;font-weight:700;cursor:default;background:{bg};"
        f"border:1px solid {border};color:{color};'{title_attr}>{html.escape(text)}</span>"
    )


def _changes_row_html(label_text, label_variant, chips_html, is_last):
    """One label+chips row of the compact strip - the label column is a
    fixed-width uppercase caption (mock: flex:0 0 150px), chips wrap
    freely next to it. No bottom border on the strip's last row (mock's
    own .chg-row:last-child rule - inline styles can't select
    ":last-child", so the caller tells us)."""
    label_colors = {"new": "#34d399", "drop": "#8aa0b8", "wait": "#fbbf24"}
    border = "" if is_last else "border-bottom:1px solid #14243d;"
    return (
        f"<div style='display:flex;align-items:flex-start;gap:10px;padding:7px 0;{border}'>"
        f"<span style='flex:0 0 150px;font-size:11.5px;font-weight:700;letter-spacing:.04em;"
        f"text-transform:uppercase;padding-top:3px;color:{label_colors[label_variant]};'>"
        f"{html.escape(label_text)}</span>"
        f"<span style='display:flex;flex-wrap:wrap;gap:6px;'>{chips_html}</span>"
        "</div>"
    )


def _changes_strip(enriched, lang):
    """Top 100 Commit 5 (25 Sep 2026, owner-reported, industry mock):
    "What changed" collapsed from a ~14-line bullet list into one
    compact 3-row chip strip - New (green) / Dropped (grey, hover for
    reason) / Awaiting first score (amber, hover explains why). Purely
    a rendering change: reuses top100_engine.pool_changes() and the
    exact same top-20-by-Value-Score "would_rank" computation
    unchanged (see this function's own git history for the original,
    identical bullet-list version of this logic) - zero touch to
    _SYSTEM_PROMPT, DIMENSIONS, weights, RUBRIC_VERSION or any cache
    key, so no existing score is invalidated or resubmitted and this
    costs nothing in API spend. Rows with zero entries are omitted
    entirely (never an empty "New (0)" row)."""
    changes = top100_engine.pool_changes()
    new, dropped = changes["new"], changes["dropped"]

    top20_by_value = sorted(enriched, key=lambda r: r["value_score"], reverse=True)[:20]
    would_rank = [
        r["ticker"] for r in top20_by_value
        if r["score_row"] is None or r["score_row"].get("not_rated")
    ]

    if not new and not dropped and not would_rank:
        st.caption(_t("changes_none", lang))
        return

    st.markdown(f"**{_t('changes_heading', lang)}**")

    rows = []
    if new:
        chips = "".join(_changes_chip_html(t, "new") for t in new)
        rows.append((_t("changes_strip_new_label", lang, n=len(new)), "new", chips))
    if dropped:
        chips = "".join(
            _changes_chip_html(d["ticker"], "drop", tooltip=_dropped_reason_text(d["reason"], lang))
            for d in dropped
        )
        rows.append((_t("changes_strip_dropped_label", lang, n=len(dropped)), "drop", chips))
    if would_rank:
        tooltip = _t("changes_strip_awaiting_tooltip", lang)
        chips = "".join(_changes_chip_html(t, "wait", tooltip=tooltip) for t in would_rank)
        rows.append((_t("changes_strip_awaiting_label", lang, n=len(would_rank)), "wait", chips))

    rows_html = "".join(
        _changes_row_html(label, variant, chips, is_last=(i == len(rows) - 1))
        for i, (label, variant, chips) in enumerate(rows)
    )
    st.markdown(
        "<div style='background:#0e1930;border:1px solid #1f3352;border-radius:12px;"
        "padding:16px 20px;margin-bottom:8px;'>"
        + rows_html
        + "<div style='color:#5b7290;font-size:11px;margin-top:9px;line-height:1.55;'>"
        + html.escape(_t("changes_strip_caption", lang)) + "</div>"
        "</div>",
        unsafe_allow_html=True,
    )


def _is_owner_for_refresh():
    """Deferred, FAIL-CLOSED owner check for the Refresh all control -
    same shape as compounder_ui.py's own Trading Cost diagnostics gate
    (deferred ai_gate/paywall_engine import, since this module has no
    top-level dependency on either) and page_admin_dashboard()'s own
    independent ai_gate.is_owner() check. ANY exception here (import
    failure, no signed-in email, anything) resolves to "not owner" -
    never the other way around."""
    try:
        import ai_gate
        import paywall_engine
        return bool(ai_gate.is_owner(paywall_engine.current_user_email()))
    except Exception:
        return False


def _handle_refresh_all_click(lang):
    """The Refresh all button's own click handler - kept as a separate,
    directly-callable function (rather than inlined into the button's
    `if st.button(...):` block) specifically so it can be unit-tested
    on its own, bypassing the button widget entirely, proving the
    SERVER-SIDE re-check refuses a non-owner even when this is reached
    directly (a forged session-state click, a stale rerun, or any other
    path that skips _render_refresh_all_control()'s own is-owner check
    before getting here). This re-check is independent of that one -
    it does not trust having already been gated by the caller. Returns
    the submitted batch id, or None (nothing to refresh, or refused)."""
    if not _is_owner_for_refresh():
        st.error("This action isn't available.")
        return None
    with st.spinner(_t("refresh_all_running", lang)):
        batch_id = top100_engine.refresh_all()
    if batch_id:
        st.success(_t("refresh_all_submitted", lang, batch_id=batch_id))
    else:
        st.info(_t("refresh_all_nothing", lang))
    return batch_id


def _render_refresh_all_control(lang):
    """Owner-only "Refresh all" button - top100_engine.refresh_all()'s
    own UI trigger (Commit 1 of the original Top 100 task built the
    engine function; nothing ever called it from the page until now).
    Two INDEPENDENT checks, on purpose: this one, before the button is
    even rendered - a non-owner sees NOTHING here at all, not a
    disabled button, not an error, no trace - and a second one inside
    _handle_refresh_all_click() itself, so a non-owner who somehow
    triggers the click callback without this render check having run
    still gets refused server-side before top100_engine.refresh_all()
    - which spends real Claude API cost - is ever called. No visitor-
    reachable path can reach refresh_all() without passing BOTH."""
    if not _is_owner_for_refresh():
        return
    if st.button(_t("refresh_all_button", lang), key="top100_refresh_all_btn"):
        _handle_refresh_all_click(lang)


def _render_sort_bar(lang):
    """Point 2's four-way sort bar, above the tabs, applying to every
    tab - one shared selection (st.segmented_control's own key= gives
    it session-state persistence across reruns), same widget/pattern
    app.py's own language picker and "new money" mode picker already
    use (st.segmented_control(...) or default_label, since the widget
    can return None if a user deselects it). Returns the active sort
    mode constant (SORT_RESEARCH by default)."""
    labels = {m: _t(f"sort_option_{m}", lang) for m in SORT_MODES}
    options = [labels[m] for m in SORT_MODES]
    default_label = labels[SORT_RESEARCH]
    st.caption(_t("sort_label", lang))
    choice_label = st.segmented_control(
        _t("sort_label", lang), options, default=default_label,
        key="top100_sort_bar", label_visibility="collapsed",
    ) or default_label
    label_to_mode = {v: k for k, v in labels.items()}
    sort_mode = label_to_mode.get(choice_label, SORT_RESEARCH)
    st.caption(_t(f"sort_caption_{sort_mode}", lang))
    return sort_mode


def render_top100_page(lang="en"):
    """The Top 100 page's full content - app.py's page_top100() calls
    this after its own owner/TOP100_PUBLIC gate (see that function's
    own docstring) and _content_page_shell()/_bump_page_view()."""
    today = _dt.datetime.now(_dt.timezone.utc).strftime("%d %b %Y")
    st.markdown(
        "<div style='color:#8aa0b8;font-size:12.5px;margin-bottom:10px;'>"
        f"{html.escape(_t('subtitle', lang, model=top100_engine.MODEL_TOP100, date=today))}</div>",
        unsafe_allow_html=True,
    )

    with st.expander(_t("methodology_heading", lang), expanded=False):
        st.markdown(f"**{_t('subtitle', lang, model=top100_engine.MODEL_TOP100, date=today)}**")
        st.markdown(_t("methodology_pipeline", lang))
        st.markdown(_t("methodology_body", lang))
        st.markdown(_t("methodology_decircularisation", lang))
        st.markdown(f"**{_t('methodology_weights_heading', lang)}**")
        # Top 100 Ranking Rework: each dimension's own weight (sums to
        # top100_engine.DIMENSION_WEIGHT_TOTAL, 83 today), rescaled onto
        # a 0-100 feel the same way composite_score() itself does -
        # simple integer rounding lands on exactly 100 for the current
        # ten weights (14+12+12+12+12+10+10+7+6+5), so the table the
        # task asked for ("the ten dimensions summing to 100") is
        # exact, not approximate, for today's weights.
        weight_lines = [
            f"- {_t(f'dim_{key}', lang)}: "
            f"{round(weight * 100.0 / top100_engine.DIMENSION_WEIGHT_TOTAL)}"
            for key, _label, weight in top100_engine.DIMENSIONS
        ]
        st.markdown("\n".join(weight_lines))
        st.markdown(f"**{_t('sort_heading', lang)}**")
        sort_lines = [
            f"- **{_t(f'sort_option_{m}', lang)}** — {_t(f'sort_caption_{m}', lang)}"
            for m in SORT_MODES
        ]
        st.markdown("\n".join(sort_lines))
        st.markdown(_t("methodology_au_extension", lang))

    _render_refresh_all_control(lang)

    pool = top100_store.current_pool()
    if not pool:
        st.info(_t("no_data", lang))
        return

    enriched = _enriched_pool()
    # Top 100 Commit 5 (25 Sep 2026, owner-reported, industry mock):
    # loaded ONCE per page render, not once per row - a plain JSON file
    # read (no live fetch), cheap enough to always compute even when
    # every ticker's tag ends up sector-only (a ticker missing from
    # this dict just gets sector alone, or no tag at all - see _industry_
    # tag_html()'s own docstring).
    finer_industry = _finer_industry_by_ticker()

    usd_count = sum(1 for r in pool if r["currency"] == "USD")
    st.caption(_t("currency_banner", lang, count=usd_count, total=len(pool)))

    _changes_strip(enriched, lang)

    sort_mode = _render_sort_bar(lang)

    tabs = st.tabs([
        _t("tab_mixed", lang), _t("tab_au", lang), _t("tab_us", lang), _t("tab_full", lang),
    ])
    with tabs[0]:
        _render_top20_tab(enriched, lang, finer_industry, sort_mode)
    with tabs[1]:
        _render_top20_australia_tab(enriched, lang, finer_industry, sort_mode)
    with tabs[2]:
        _render_top20_tab(enriched, lang, finer_industry, sort_mode, currency="USD")
    with tabs[3]:
        _render_full100_tab(enriched, lang, finer_industry, sort_mode)
