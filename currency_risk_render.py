"""
currency_risk_render.py

Rendering for the 💱 Currency Risk page - a thin app.py page function
calls render_currency_risk_page() here, same "*_engine.py owns the
logic, *_render.py owns markup" split top100_engine.py/top100_render.py
already established. Reads currency_risk_engine.py directly (no
Streamlit dependency in that module). Pure Python, no AI calls, $0
ongoing beyond one cached-daily yfinance history call per FX pair.

Owner-approved mock (25 Sep 2026, "headwind_and_currency_risk_mock.html",
section ②): pick two currencies (default AUD -> USD, since most of the
Top 100 is USD-priced), a range (5y/10y/20y/Max), and see (A) where the
rate sits in its own history, (B) the distribution of rolling 12-month
changes, (C) what a given position size stands to gain/lose from the FX
leg alone under three scenarios.
"""
import html

import plotly.graph_objects as go
import streamlit as st

import compounder_ui
import currency_risk_engine as cre
import i18n


def _t(key, lang, **fmt):
    return i18n.t(f"currency_risk.{key}", lang, **fmt)


def _tile_html(label, value, color=None):
    color_style = f"color:{color};" if color else "color:#e6edf5;"
    return (
        "<div style='background:#0b1526;border:1px solid #1a2b4a;border-radius:10px;"
        "padding:9px 12px;'>"
        f"<div style='color:#8aa0b8;font-size:10px;letter-spacing:.06em;"
        f"text-transform:uppercase;'>{html.escape(label)}</div>"
        "<div style='font-family:ui-monospace,Menlo,SFMono-Regular,monospace;"
        f"font-size:15px;font-weight:800;margin-top:3px;{color_style}'>{value}</div>"
        "</div>"
    )


def _tiles_grid_html(tiles):
    """Responsive tile grid - repeat(auto-fit,minmax(...)) rather than a
    fixed column count, same "wraps naturally on a phone, no separate
    media query" convention as app.py's own .sdd-strip grid."""
    return (
        "<div style='display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));"
        "gap:10px;margin-top:10px;'>" + "".join(tiles) + "</div>"
    )


def _render_selector_bar(lang):
    """Pair selector (two dropdowns, same-currency guarded downstream)
    + range chips - one shared state (st.selectbox/segmented_control's
    own key= persistence), read fresh on every rerun."""
    cols = st.columns([1, 1, 2])
    with cols[0]:
        base = st.selectbox(
            _t("base_label", lang), cre.CURRENCIES,
            index=cre.CURRENCIES.index(cre.DEFAULT_BASE), key="cr_base",
        )
    with cols[1]:
        quote = st.selectbox(
            _t("quote_label", lang), cre.CURRENCIES,
            index=cre.CURRENCIES.index(cre.DEFAULT_QUOTE), key="cr_quote",
        )
    with cols[2]:
        labels = {k: _t(f"range_{k}", lang) for k in cre.RANGE_KEYS}
        options = [labels[k] for k in cre.RANGE_KEYS]
        default_label = labels[cre.DEFAULT_RANGE]
        st.caption(_t("range_label", lang))
        choice_label = st.segmented_control(
            _t("range_label", lang), options, default=default_label,
            key="cr_range", label_visibility="collapsed",
        ) or default_label
        label_to_key = {v: k for k, v in labels.items()}
        range_key = label_to_key.get(choice_label, cre.DEFAULT_RANGE)
    return base, quote, range_key


def _history_chart_text_equivalent(base, quote, stats):
    return (
        f"{base}/{quote} rate history: today {stats['today']:.4f}, period average "
        f"{stats['average']:.4f} ({stats['pct_vs_average']:+.1f}% vs average), range "
        f"{stats['range_min']:.4f} to {stats['range_max']:.4f}, {stats['percentile']:.0f}th "
        f"percentile, annualised volatility {stats['annualised_vol_pct']:.1f}%."
    )


def _render_history_chart(dates, closes, stats, base, quote, lang):
    avg, sigma = stats["average"], stats["sigma"]
    fig = go.Figure()
    # ±1σ band (behind everything else - added first).
    fig.add_trace(go.Scatter(
        x=list(dates) + list(dates[::-1]),
        y=[avg + sigma] * len(dates) + [avg - sigma] * len(dates),
        fill="toself", fillcolor="rgba(45,212,191,0.10)", line=dict(width=0),
        hoverinfo="skip", showlegend=False, name=_t("band_label", lang),
    ))
    # Dashed period-average line - trace name is internal only
    # (showlegend=False, hoverinfo="skip"), never rendered to the
    # visitor, so it doesn't need the templated {average} label the
    # visible annotation below uses.
    fig.add_trace(go.Scatter(
        x=[dates[0], dates[-1]], y=[avg, avg], mode="lines",
        line=dict(color="#fbbf24", width=1.5, dash="dash"),
        hoverinfo="skip", showlegend=False, name="average",
    ))
    fig.add_annotation(
        x=dates[-1], y=avg, text=_t("avg_line_label", lang, average=f"{avg:.4f}"),
        showarrow=False, xanchor="left", xshift=8, font=dict(size=10, color="#fbbf24"),
    )
    # The rate line itself - site convention: 2px, teal.
    fig.add_trace(go.Scatter(
        x=dates, y=closes, mode="lines", line=dict(color="#2dd4bf", width=2),
        name=f"{base}/{quote}",
    ))
    # Current-rate marker + direct end label (site convention, replacing
    # a legend - see app.py's own _render_portfolio_value_chart()).
    fig.add_trace(go.Scatter(
        x=[dates[-1]], y=[closes[-1]], mode="markers",
        marker=dict(size=7, color="#0b1220", line=dict(width=2, color="#2dd4bf")),
        showlegend=False, hoverinfo="skip",
    ))
    fig.add_annotation(
        x=dates[-1], y=closes[-1], text=f"{closes[-1]:.4f}", showarrow=False,
        xanchor="left", xshift=8, yshift=14, font=dict(size=11, color="#2dd4bf"),
    )
    fig.update_layout(
        margin=dict(t=20, b=10, l=10, r=90), height=340,
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        font=dict(color="#c7d2e0"), hovermode="x unified", showlegend=False,
        xaxis=dict(showgrid=False),
        yaxis=dict(showgrid=True, gridcolor="rgba(138,160,184,0.15)"),
    )
    compounder_ui.sdd_plotly_chart(
        fig, text_description=_history_chart_text_equivalent(base, quote, stats),
    )


def _ordinal_suffix_en(n):
    n = int(n)
    if 11 <= (n % 100) <= 13:
        return "th"
    return {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")


def _percentile_text(percentile, lang):
    n = round(percentile)
    if lang == "es":
        return _t("percentile_value_es", lang, n=n)
    return _t("percentile_value_en", lang, n=n, suffix=_ordinal_suffix_en(n))


def _render_section_a(dates, closes, stats, base, quote, lang):
    st.markdown(f"### {_t('section_a_heading', lang)}")
    st.caption(_t("section_a_why", lang))
    _render_history_chart(dates, closes, stats, base, quote, lang)
    tiles = [
        _tile_html(_t("tile_today", lang), f"{stats['today']:.4f}"),
        _tile_html(_t("tile_average", lang), f"{stats['average']:.4f}", "#fbbf24"),
        _tile_html(
            _t("tile_vs_average", lang), f"{stats['pct_vs_average']:+.1f}%",
            "#34d399" if stats["pct_vs_average"] >= 0 else "#fb7185",
        ),
        _tile_html(_t("tile_range", lang), f"{stats['range_min']:.4f} – {stats['range_max']:.4f}"),
        _tile_html(_t("tile_percentile", lang), _percentile_text(stats["percentile"], lang)),
        _tile_html(
            _t("tile_vol", lang),
            f"{stats['annualised_vol_pct']:.1f}%" if stats["annualised_vol_pct"] is not None else "—",
        ),
    ]
    st.markdown(_tiles_grid_html(tiles), unsafe_allow_html=True)
    st.caption(_t("section_a_caption", lang))


_BUCKET_ORDER = [k for k, _lo, _hi in cre.ROLLING_BUCKETS]
# Sign convention (verified against the mock's own section C worked
# numbers - see currency_risk_engine.position_impact()'s own docstring):
# a POSITIVE rolling change means the base currency strengthened - a
# drag on a foreign (quote-denominated) holding - so positive-change
# buckets are RED; a negative change (base weakened) is a tailwind, so
# negative-change buckets are GREEN. (The mock's own decorative SVG bar
# colours read the other way round from its own caption text - an
# inconsistency in that illustrative mock, not reproduced here; this
# implementation follows the task's own explicit "red = base currency
# strengthened / drag... green = tailwind" wording, which the verified
# section C math above also confirms.)
_BUCKET_COLOR = {
    "lt_m10": "#34d399", "m10_m5": "#34d399", "m5_0": "#34d399",
    "0_p5": "#fb7185", "p5_p10": "#fb7185", "gt_p10": "#fb7185",
}


def _render_distribution_chart(dist, lang):
    labels = [_t(f"bucket_{k}", lang) for k in _BUCKET_ORDER]
    pct_values = [dist["bucket_pct"][k] for k in _BUCKET_ORDER]
    colors = [_BUCKET_COLOR[k] for k in _BUCKET_ORDER]
    counts = [dist["bucket_counts"][k] for k in _BUCKET_ORDER]
    text = [f"{v:.0f}%" for v in pct_values]
    fig = go.Figure(go.Bar(
        x=labels, y=pct_values, marker_color=colors, text=text, textposition="outside",
        hovertext=[f"{c} of {dist['n_windows']} 12-month windows" for c in counts],
        hovertemplate="%{hovertext}<extra></extra>",
    ))
    fig.update_layout(
        margin=dict(t=10, b=10, l=10, r=10), height=260,
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        font=dict(color="#c7d2e0"),
        xaxis=dict(showgrid=False), yaxis=dict(showgrid=True, gridcolor="rgba(138,160,184,0.15)",
                                                title=_t("bucket_y_axis", lang)),
    )
    compounder_ui.sdd_plotly_chart(
        fig,
        text_description=(
            f"Distribution of rolling 12-month changes across {dist['n_windows']} windows: "
            + ", ".join(f"{_t(f'bucket_{k}', lang)} {dist['bucket_pct'][k]:.0f}%" for k in _BUCKET_ORDER)
        ),
    )


def _render_section_b(dist, lang):
    st.markdown(f"### {_t('section_b_heading', lang)}")
    st.caption(_t("section_b_why", lang))
    _render_distribution_chart(dist, lang)
    tiles = [
        _tile_html(_t("tile_worst", lang), f"{dist['worst']:+.1f}%", "#fb7185"),
        _tile_html(_t("tile_best", lang), f"{dist['best']:+.1f}%", "#34d399"),
        _tile_html(_t("tile_swing", lang), f"±{dist['typical_swing']:.1f}%"),
    ]
    st.markdown(_tiles_grid_html(tiles), unsafe_allow_html=True)
    st.caption(_t("section_b_caption", lang))


def _render_section_c(base, quote, current_rate, average, sigma, lang):
    st.markdown(f"### {_t('section_c_heading', lang)}")
    st.caption(_t("section_c_why", lang, base=base))
    position = st.number_input(
        _t("position_label", lang, base=base), min_value=0.0, value=50000.0, step=1000.0,
        format="%.0f", key="cr_position_size",
    )
    impacts = {imp["key"]: imp for imp in cre.position_impact(position, current_rate, average, sigma)}
    tiles = []
    for key, label_key in (
        ("average", "scenario_average_label"),
        ("plus_1sigma", "scenario_plus_1sigma_label"),
        ("minus_1sigma", "scenario_minus_1sigma_label"),
    ):
        imp = impacts.get(key)
        if not imp:
            continue
        label = _t(label_key, lang, base=base, rate=f"{imp['scenario_rate']:.4f}")
        color = "#34d399" if imp["pct_change"] >= 0 else "#fb7185"
        sign = "+" if imp["amount_change"] >= 0 else "−"
        value = f"{imp['pct_change']:+.1f}% · {sign}{base}${abs(imp['amount_change']):,.0f}"
        tiles.append(_tile_html(label, value, color))
    st.markdown(_tiles_grid_html(tiles), unsafe_allow_html=True)
    st.caption(_t("section_c_caption", lang))


def render_currency_risk_page(lang="en"):
    """The Currency Risk page's full content - app.py's page_currency_
    risk() calls this after its own _content_page_shell()/_bump_page_
    view() (same split as top100_render.render_top100_page())."""
    st.markdown(
        "<div style='color:#8aa0b8;font-size:12.5px;margin-bottom:10px;'>"
        f"{html.escape(_t('subtitle', lang))}</div>",
        unsafe_allow_html=True,
    )
    st.caption(_t("disclaimer", lang))

    base, quote, range_key = _render_selector_bar(lang)
    if base == quote:
        st.warning(_t("same_currency_warning", lang))
        return

    history = cre.get_fx_history(base, quote)
    if not history:
        st.info(_t("no_data", lang, base=base, quote=quote))
        return
    if history["stale"]:
        st.caption(_t("stale_warning", lang))

    dates, closes = cre.slice_range(history["dates"], history["closes"], range_key)
    stats = cre.period_stats(closes)
    if not stats:
        st.info(_t("no_data", lang, base=base, quote=quote))
        return

    st.markdown(
        f"<div style='font-size:26px;font-weight:800;margin:6px 0 14px;color:#e6edf5;'>"
        f"{stats['today']:.4f} <span style='font-size:12px;color:#8aa0b8;font-weight:400;'>"
        f"{html.escape(_t('hero_caption', lang, quote=quote, base=base))}</span></div>",
        unsafe_allow_html=True,
    )

    _render_section_a(dates, closes, stats, base, quote, lang)

    dist = cre.rolling_12m_distribution(closes)
    if dist:
        _render_section_b(dist, lang)
    else:
        st.caption(_t("not_enough_data_for_distribution", lang))

    _render_section_c(base, quote, stats["today"], stats["average"], stats["sigma"], lang)
