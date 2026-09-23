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


def _t(key, lang, **fmt):
    return i18n.t(f"top100.{key}", lang, **fmt)


def _score_chip_html(label, score):
    """One dimension chip - the score number colour-banded (1-2 red,
    3 amber, 4-5 green - the task's own "colour-banded per the mock"),
    a muted grey "—" chip when the dimension itself is null (never a
    fabricated colour for a reading that doesn't exist)."""
    if score is None:
        color, bg, text = "#5b7290", "#1a2332", "—"
    else:
        band = _SCORE_BAND_COLOR[score]
        color = compounder_ui._CP_COLOR_TEXT[band]
        bg = compounder_ui._CP_COLOR_FILL[band]
        text = str(score)
    return (
        "<span style='display:inline-flex;align-items:center;gap:4px;border-radius:8px;"
        f"padding:3px 8px;margin:2px 4px 2px 0;font-size:11px;background:{bg};color:{color};"
        "font-weight:700;'>"
        f"{html.escape(label)} <span style='font-family:ui-monospace,Menlo,SFMono-Regular,monospace;'>"
        f"{html.escape(text)}</span></span>"
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


def _render_row(rank, row, lang):
    """One company row - rank/ticker(linked)/Value Score/MOS/composite,
    the ten dimension chips, a tradability chip when its latest
    recorded spread is wide, and a "why it's here / what to check"
    note in an expander (also where the ten chips' own justifications
    + source periods live, one expander per row rather than ten, so a
    100-row tab stays light)."""
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
    if composite is not None:
        header_bits.append(
            f"<span style='color:#8aa0b8;font-size:12px;'>{html.escape(_t('col_composite', lang))}: "
            f"<b style='color:#34d399;'>{composite:.1f}</b></span>"
        )
    if not_rated:
        header_bits.append(_not_rated_chip_html(lang))

    spread_pct = _tradability_spread_pct(ticker)
    tradable_chip = ""
    if spread_pct is not None and spread_pct > TRADABILITY_SPREAD_THRESHOLD_PCT:
        tradable_chip = _tradability_chip_html(spread_pct, lang)

    st.markdown(
        "<div style='background:#0b1526;border:1px solid #1a2b4a;border-radius:10px;"
        "padding:10px 14px;margin-bottom:8px;'>"
        "<div style='display:flex;flex-wrap:wrap;align-items:center;gap:12px;'>"
        + "".join(header_bits) + tradable_chip +
        "</div>"
        "<div style='margin-top:8px;'>"
        + "".join(
            _score_chip_html(_t(f"dim_{key}", lang), (score_row["dims"].get(key) or {}).get("score")
                              if score_row else None)
            for key in top100_engine.DIMENSION_KEYS
        )
        + "</div>"
        "</div>",
        unsafe_allow_html=True,
    )

    # v2 amendment, Commit 1 compatibility note: `summary` no longer
    # exists on a score row (top100_engine's v2 response schema dropped
    # it - see that module's docstring). Commit 2 replaces this whole
    # note with the inversion line; until it lands, a rated company
    # simply gets no caption here rather than the actively-misleading
    # "insufficient public record" text a naive `or` fallback would
    # show for a company that in fact IS fully rated.
    note = _t("not_rated_note", lang) if not_rated else None
    with st.expander(f"{ticker} — {_t('why_here_label', lang)}", expanded=False):
        if note:
            st.caption(note)
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
        st.markdown(_t("methodology_body", lang))
        st.markdown(f"**{_t('methodology_weights_heading', lang)}**")
        weight_lines = [f"- {_t('col_composite', lang)} ({_t('methodology_return_label', lang)}): {top100_engine.WEIGHT_RETURN}"]
        for key, label, weight in top100_engine.DIMENSIONS:
            weight_lines.append(f"- {_t(f'dim_{key}', lang)}: {weight}")
        st.markdown("\n".join(weight_lines))

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
