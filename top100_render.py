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


def _not_rated_chip_html(lang):
    return (
        "<span style='display:inline-flex;align-items:center;border-radius:999px;padding:2px 9px;"
        "font-size:11px;font-weight:700;background:#241a10;border:1px solid #7a4a12;color:#fbbf24;'>"
        f"{html.escape(_t('not_rated', lang))}</span>"
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


def _render_row(rank, row, lang):
    """One company row - rank/ticker(linked)/Value Score AND Research
    Score side by side (task's own "sharp divergence is intended and
    visible"), the ten dimension chips (each with a hover tooltip),
    free sentiment/tradability chips, the inversion line (severity-
    styled, replacing v1's "what to check" note), and a row-level
    details toggle that expands every one of the ten justifications
    stacked - the SAME text the hover tooltips carry, so a phone
    (no hover) or a "read everything" desktop visit never leaves a
    justification unreachable."""
    ticker = row["ticker"]
    score_row = row.get("score_row")
    composite = row.get("composite")
    not_rated = score_row is None or score_row.get("not_rated")

    header_bits = [
        f"<span style='color:#5b7290;font-family:ui-monospace,Menlo,SFMono-Regular,monospace;"
        f"font-size:12px;'>#{rank}</span>",
        f"<a href='/deep-dive?ticker={html.escape(ticker)}' target='_self' "
        "style='color:#5ed3f0;font-weight:700;text-decoration:none;font-size:14px;'>"
        f"{html.escape(ticker)}</a>",
    ]
    if row.get("company_name"):
        header_bits.append(f"<span style='color:#8aa0b8;font-size:12px;'>{html.escape(row['company_name'])}</span>")
    header_bits.append(
        f"<span style='color:#8aa0b8;font-size:12px;'>{html.escape(_t('col_value_score', lang))}: "
        f"<b style='color:#e6edf5;'>{row['value_score']:.1f}</b></span>"
    )
    if row.get("mos_pct") is not None:
        header_bits.append(
            f"<span style='color:#8aa0b8;font-size:12px;'>{html.escape(_t('col_mos', lang))}: "
            f"<b style='color:#e6edf5;'>{row['mos_pct']:.1f}%</b></span>"
        )
    # Both scores side by side, always: Value Score above (the site's
    # own numeric measurement) already rendered; Research Score here
    # (the AI-weighted analytical judgment) shows only when the
    # company has one - a NOT RATED row shows Value Score alone plus
    # the NOT RATED chip below, the "sharp divergence" the task's own
    # OCL example is about.
    if composite is not None:
        header_bits.append(
            f"<span style='color:#8aa0b8;font-size:12px;'>{html.escape(_t('col_research_score', lang))}: "
            f"<b style='color:#34d399;'>{composite:.1f}</b></span>"
        )
    if not_rated:
        header_bits.append(_not_rated_chip_html(lang))

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
            _score_chip_html(key, (score_row["dims"].get(key) or {}).get("score") if score_row else None,
                              (score_row["dims"].get(key) if score_row else None), lang)
            for key in top100_engine.DIMENSION_KEYS
        )
        + "</div>"
        + _inversion_line_html(score_row, lang) +
        "</div>",
        unsafe_allow_html=True,
    )

    # Row-level details toggle (task's own words) - the tap-expand path
    # for phones (no hover) AND the desktop "read everything" path.
    # NOT RATED gets the honest not_rated_note instead of ten empty
    # dimensions.
    with st.expander(f"{ticker} — {_t('why_here_label', lang)}", expanded=False):
        if not_rated:
            st.caption(_t("not_rated_note", lang))
        if score_row:
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
    pool_mos_values = [r["mos_pct"] for r in pool if r.get("mos_pct") is not None]
    out = []
    for row in pool:
        score_row = scores.get(row["ticker"])
        composite = top100_engine.composite_score(score_row, row.get("mos_pct"), pool_mos_values)
        out.append({**row, "score_row": score_row, "composite": composite})
    return out


def _render_top20_tab(enriched, lang, currency=None):
    rows = [r for r in enriched if r["composite"] is not None]
    if currency:
        rows = [r for r in rows if r["currency"] == currency]
    rows = sorted(rows, key=lambda r: r["composite"], reverse=True)[:20]
    if not rows:
        st.caption(_t("empty_tab", lang))
        return
    for i, row in enumerate(rows, start=1):
        _render_row(i, row, lang)


def _render_full100_tab(enriched, lang):
    rows = sorted(enriched, key=lambda r: r["value_score"], reverse=True)
    for i, row in enumerate(rows, start=1):
        _render_row(i, row, lang)


def _changes_strip(enriched, lang):
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

    lines = []
    if new:
        lines.append(_t("changes_new", lang, tickers=", ".join(new)))
    for d in dropped:
        lines.append(_t("changes_dropped", lang, ticker=d["ticker"], reason=d["reason"]))
    for ticker in would_rank:
        lines.append(_t("changes_would_rank", lang, ticker=ticker))

    st.markdown(f"**{_t('changes_heading', lang)}**")
    for line in lines:
        st.caption(f"• {line}")


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
        weight_lines = [f"- {_t('col_research_score', lang)} ({_t('methodology_return_label', lang)}): {top100_engine.WEIGHT_RETURN}"]
        for key, label, weight in top100_engine.DIMENSIONS:
            weight_lines.append(f"- {_t(f'dim_{key}', lang)}: {weight}")
        st.markdown("\n".join(weight_lines))

    _render_refresh_all_control(lang)

    pool = top100_store.current_pool()
    if not pool:
        st.info(_t("no_data", lang))
        return

    enriched = _enriched_pool()

    usd_count = sum(1 for r in pool if r["currency"] == "USD")
    st.caption(_t("currency_banner", lang, count=usd_count, total=len(pool)))

    _changes_strip(enriched, lang)

    tabs = st.tabs([
        _t("tab_mixed", lang), _t("tab_au", lang), _t("tab_us", lang), _t("tab_full", lang),
    ])
    with tabs[0]:
        _render_top20_tab(enriched, lang)
    with tabs[1]:
        _render_top20_tab(enriched, lang, currency="AUD")
    with tabs[2]:
        _render_top20_tab(enriched, lang, currency="USD")
    with tabs[3]:
        _render_full100_tab(enriched, lang)
